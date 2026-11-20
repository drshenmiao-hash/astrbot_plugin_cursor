"""Model resolution and native registration from simulated ACP session/new replies."""

import sys
import unittest
from copy import deepcopy
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

from cursor_plugin import runtime
from cursor_plugin.acp import ACPClient
from cursor_plugin.engine import Engine
from cursor_plugin.errors import CursorError
from cursor_plugin.models import ModelResolutionError, resolve_model
from tests.test_registration import Manager, NativeModel, existing_config

CATALOG = [
    {"modelId": "auto", "name": "Automatic"},
    {"modelId": "grok-4.6", "name": "Grok 4.6"},
    {"modelId": "sonnet-4.6", "name": "Sonnet 4.6"},
]
_REAL_FETCH = runtime._fetch_available_models


def memory_acp(catalog):
    client = ACPClient.__new__(ACPClient)
    client.ready = {"cwd": "/work"}
    client.session_models = {}

    async def rpc(method, params, *, timeout=None):
        if method == "session/new":
            return {
                "sessionId": "memory-session",
                "models": {"availableModels": deepcopy(catalog())},
                "modes": {"availableModes": [{"id": "ask"}]},
            }
        if method == "session/set_config_option":
            return {"configOptions": [{"id": "model", "currentValue": params["value"]}]}
        if method == "session/set_mode":
            return {}
        raise AssertionError(f"Unexpected RPC; no prompts allowed: {method}")

    client.call = AsyncMock(side_effect=rpc)
    return client


class ModelResolutionTests(unittest.TestCase):
    def test_exact_model_id_wins_over_other_display_name(self):
        catalog = CATALOG + [{"modelId": "another-model", "name": "grok-4.6"}]
        self.assertEqual(resolve_model("grok-4.6", catalog), "grok-4.6")

    def test_management_id_maps_grok_to_actual_acp_id(self):
        self.assertEqual(resolve_model("cursor-grok-4.6-high", CATALOG), "grok-4.6")

    def test_display_name_maps_uniquely(self):
        self.assertEqual(resolve_model("Grok 4.6", CATALOG), "grok-4.6")
        self.assertEqual(resolve_model(" grok 4.6 ", CATALOG), "grok-4.6")
        self.assertEqual(
            resolve_model("Display", [{"modelId": "grok-4.6", "displayName": "Display"}]),
            "grok-4.6",
        )

    def test_duplicate_display_name_is_ambiguous(self):
        with self.assertRaises(ModelResolutionError) as caught:
            resolve_model("Grok 4.6", CATALOG + [{"modelId": "other-grok", "name": "Grok 4.6"}])
        self.assertEqual(caught.exception.code, "model_ambiguous")
        self.assertEqual(caught.exception.candidates, ("grok-4.6", "other-grok"))

    def test_management_alias_conflict_is_ambiguous(self):
        with self.assertRaises(ModelResolutionError) as caught:
            resolve_model(
                "cursor-grok-4.6-high",
                CATALOG + [{"modelId": "grok-4.6-high", "name": "Grok High"}],
            )
        self.assertEqual(caught.exception.code, "model_ambiguous")

    def test_missing_model_reports_available_ids(self):
        with self.assertRaises(ModelResolutionError) as caught:
            resolve_model("missing-model", CATALOG)
        self.assertEqual(caught.exception.code, "model_unavailable")
        for model_id in ["auto", "grok-4.6", "sonnet-4.6"]:
            self.assertIn(model_id, str(caught.exception))
            self.assertIn(model_id, caught.exception.available_model_ids)

    def test_auto_empty_default_and_auto_display_remain_forbidden(self):
        for requested in [
            "",
            " ",
            "auto",
            "AUTO",
            "default",
            "auto-smart",
            "Automatic",
            "cursor-auto",
        ]:
            with (
                self.subTest(requested=requested),
                self.assertRaises(ModelResolutionError) as caught,
            ):
                resolve_model(requested, CATALOG)
            self.assertEqual(caught.exception.code, "model")

    def test_arbitrary_suffix_is_not_guessed(self):
        with self.assertRaises(ModelResolutionError) as caught:
            resolve_model("cursor-grok-4.6-ultra", CATALOG)
        self.assertEqual(caught.exception.code, "model_unavailable")

    def test_malformed_catalog_is_protocol_error_not_model_unavailable(self):
        for catalog in [None, [{}], [{"modelId": "bad\nvalue"}]]:
            with self.subTest(catalog=catalog), self.assertRaises(CursorError) as caught:
                resolve_model("grok-4.6", catalog)
            self.assertEqual(caught.exception.code, "protocol")


class ACPModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_catalog_uses_one_session_new_and_no_prompt(self):
        client = memory_acp(lambda: CATALOG)
        self.assertEqual(await client.available_models(), CATALOG)
        self.assertEqual([c.args[0] for c in client.call.await_args_list], ["session/new"])

    async def test_actual_set_config_option_uses_canonical_id(self):
        client = memory_acp(lambda: CATALOG)
        self.assertEqual(await client.new_session("grok-4.6"), "memory-session")
        calls = client.call.await_args_list
        self.assertEqual(
            [c.args[0] for c in calls],
            ["session/new", "session/set_config_option", "session/set_mode"],
        )
        self.assertEqual(calls[1].args[1]["value"], "grok-4.6")
        self.assertEqual(calls[2].args[1]["modeId"], "ask")

    async def test_unavailable_validated_id_is_never_reinterpreted_as_an_alias(self):
        for canonical, replacement in [
            ("grok-4.6", {"modelId": "different-model", "displayName": "grok-4.6"}),
            ("cursor-grok-4.6-high", {"modelId": "grok-4.6"}),
        ]:
            with self.subTest(canonical=canonical):
                resolved = resolve_model(canonical, [{"modelId": canonical}])
                client = memory_acp(lambda: [replacement])
                with (
                    self.assertLogs("astrbot", level="WARNING"),
                    self.assertRaises(ModelResolutionError) as caught,
                ):
                    await client.new_session(resolved)
                self.assertEqual(caught.exception.code, "model_unavailable")
                self.assertEqual(client.session_models, {})
                self.assertEqual(
                    [call.args[0] for call in client.call.await_args_list], ["session/new"]
                )

    async def test_missing_model_logs_ids_without_setting_another_model(self):
        client = memory_acp(lambda: CATALOG)
        with (
            self.assertLogs("astrbot", level="WARNING") as logs,
            self.assertRaises(ModelResolutionError),
        ):
            await client.new_session("missing-model")
        self.assertIn("grok-4.6", "\n".join(logs.output))
        self.assertIn("sonnet-4.6", "\n".join(logs.output))
        self.assertEqual([c.args[0] for c in client.call.await_args_list], ["session/new"])

    async def test_ambiguous_model_never_sets_model(self):
        client = memory_acp(lambda: CATALOG + [{"modelId": "other-grok", "name": "Grok 4.6"}])
        with (
            self.assertLogs("astrbot", level="WARNING"),
            self.assertRaises(ModelResolutionError) as caught,
        ):
            await client.new_session("Grok 4.6", resolve_alias=True)
        self.assertEqual(caught.exception.code, "model_ambiguous")
        self.assertEqual(client.call.await_count, 1)


class RuntimeModelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old = runtime._settings, runtime._engine, runtime._available_models
        runtime._engine = None
        runtime.configure({"model_id": "cursor-grok-4.6-high"})
        self.catalog = deepcopy(CATALOG)
        self.acp = memory_acp(lambda: self.catalog)
        self.fetch = AsyncMock(side_effect=self.acp.available_models)
        self.fetch_patch = patch.object(runtime, "_fetch_available_models", self.fetch)
        self.fetch_patch.start()
        self.addCleanup(self.fetch_patch.stop)
        provider = ModuleType("cursor_plugin.provider")
        provider.CursorCLIProvider = NativeModel
        module_patch = patch.dict(sys.modules, {"cursor_plugin.provider": provider})
        module_patch.start()
        self.addCleanup(module_patch.stop)
        proc = patch(
            "asyncio.create_subprocess_exec",
            side_effect=AssertionError("No real processes allowed"),
        )
        proc.start()
        self.addCleanup(proc.stop)
        self.config = existing_config()
        self.manager = Manager(self.config)
        self.context = SimpleNamespace(
            astrbot_config_mgr=SimpleNamespace(default_conf=self.config),
            provider_manager=self.manager,
        )

    def tearDown(self):
        runtime._settings, runtime._engine, runtime._available_models = self.old

    def cursor_models(self):
        return [m for m in self.config["provider"] if m.get("type") == "cursor_cli"]

    async def test_startup_registers_canonical_ids_from_one_acp_catalog(self):
        await runtime.initialize(self.context)
        model = self.cursor_models()[0]
        self.assertEqual(model["id"], "grok-4.6")
        self.assertEqual(model["model"], "grok-4.6")
        self.assertEqual(runtime.settings()["model"], "grok-4.6")
        self.assertEqual(runtime.settings()["requested_model"], "cursor-grok-4.6-high")
        self.assertEqual(await runtime.available_model_ids(), ["grok-4.6", "sonnet-4.6"])
        self.fetch.assert_awaited_once()
        self.assertEqual(self.acp.call.await_count, 1)

    async def test_reload_fetches_again_without_duplicate_registration(self):
        await runtime.initialize(self.context)
        await runtime.initialize(self.context)
        self.assertEqual(self.fetch.await_count, 2)
        self.assertEqual(self.acp.call.await_count, 2)
        self.assertEqual(len(self.cursor_models()), 1)
        self.assertEqual(len(self.config.saved), 1)

    async def test_reload_uses_new_catalog_and_does_not_fallback(self):
        await runtime.initialize(self.context)
        before = deepcopy(dict(self.config))
        selected = self.manager.curr_provider_inst
        self.catalog = [{"modelId": "sonnet-4.6", "name": "Sonnet 4.6"}]
        with (
            self.assertLogs("astrbot", level="WARNING"),
            self.assertRaises(ModelResolutionError) as caught,
        ):
            await runtime.initialize(self.context)
        self.assertEqual(caught.exception.code, "model_unavailable")
        self.assertEqual(dict(self.config), before)
        self.assertIs(self.manager.curr_provider_inst, selected)
        self.assertEqual(self.manager.reloads, ["grok-4.6"])

    async def test_display_name_configuration_is_accepted_and_mapped(self):
        runtime.configure({"model_id": "Grok 4.6"})
        await runtime.initialize(self.context)
        self.assertEqual(self.cursor_models()[0]["model"], "grok-4.6")
        self.assertEqual(self.cursor_models()[0]["id"], "grok-4.6")

    async def test_existing_canonical_alias_keeps_name_default_and_fallbacks(self):
        self.config["provider_sources"].append({"id": "custom-cursor", "type": "cursor_cli"})
        alias = {
            "id": "my-cursor-alias",
            "model": "grok-4.6",
            "enable": True,
            "provider_source_id": "custom-cursor",
        }
        self.config["provider"].append(alias)
        self.manager.inst_map[alias["id"]] = NativeModel(alias)
        self.manager.curr_provider_inst = self.manager.inst_map[alias["id"]]
        self.config["agent"]["config"]["model"]["provider_id"] = alias["id"]
        self.config["provider_settings"]["fallback_chat_models"].append(alias["id"])
        defaults = deepcopy(self.config["agent"]), deepcopy(self.config["provider_settings"])
        await runtime.initialize(self.context)
        self.assertEqual([m["id"] for m in self.cursor_models()], [alias["id"]])
        self.assertIs(self.manager.curr_provider_inst, self.manager.inst_map[alias["id"]])
        self.assertEqual((self.config["agent"], self.config["provider_settings"]), defaults)

    async def test_legacy_management_model_value_is_updated_without_renaming(self):
        self.config["provider_sources"].append({"id": "custom-cursor", "type": "cursor_cli"})
        self.config["provider"].append(
            {
                "id": "old-name",
                "model": "cursor-grok-4.6-high",
                "enable": True,
                "provider_source_id": "custom-cursor",
            }
        )
        await runtime.initialize(self.context)
        self.assertEqual(
            [(m["id"], m["model"]) for m in self.cursor_models()], [("old-name", "grok-4.6")]
        )

    async def test_missing_model_logs_catalog_and_leaves_config_untouched(self):
        runtime.configure({"model_id": "missing-model"})
        before = deepcopy(dict(self.config))
        with (
            self.assertLogs("astrbot", level="WARNING") as logs,
            self.assertRaises(ModelResolutionError) as caught,
        ):
            await runtime.initialize(self.context)
        self.assertEqual(caught.exception.code, "model_unavailable")
        for model_id in ["grok-4.6", "sonnet-4.6"]:
            self.assertIn(model_id, "\n".join(logs.output))
        self.assertEqual(dict(self.config), before)
        self.assertEqual(self.manager.reloads, [])

    async def test_auto_empty_default_create_no_model_and_do_not_discover(self):
        before = deepcopy(self.config["provider"])
        for requested in ["auto", "", "default"]:
            runtime.configure({"model_id": requested})
            with self.assertLogs("astrbot", level="WARNING"):
                await runtime.initialize(self.context)
        self.fetch.assert_not_awaited()
        self.acp.call.assert_not_awaited()
        self.assertEqual(self.config["provider"], before)
        self.assertEqual(self.manager.reloads, [])

    async def test_default_oauth_deepseek_and_fallbacks_are_untouched(self):
        before = deepcopy(dict(self.config))
        selected = self.manager.curr_provider_inst
        await runtime.initialize(self.context)
        self.assertIs(self.manager.curr_provider_inst, selected)
        self.assertEqual(self.config["provider_sources"][:2], before["provider_sources"])
        self.assertEqual(self.config["provider"][:2], before["provider"])
        for key in ["agent", "provider_settings", "session_preferences"]:
            self.assertEqual(self.config[key], before[key])

    async def test_discovery_reuses_live_engine_without_starting_process(self):
        engine = Engine(runtime.settings())
        peer = SimpleNamespace(closed=False, available_models=AsyncMock(return_value=CATALOG))
        engine.client = peer
        runtime._engine = engine
        with patch.object(runtime, "Engine", side_effect=AssertionError("Must reuse live engine")):
            self.assertEqual(await _REAL_FETCH(), CATALOG)
        peer.available_models.assert_awaited_once()
        self.assertEqual(engine.session_slots, 1)
        # No process/owner was created; detach the memory peer before teardown.
        engine.client = None
        await engine.close()

    async def test_standalone_probe_is_closed_even_on_discovery_failure(self):
        runtime._engine = None
        probe = SimpleNamespace(
            available_models=AsyncMock(side_effect=CursorError("not_logged_in")),
            close=AsyncMock(),
        )
        with (
            patch.object(runtime, "Engine", return_value=probe),
            self.assertRaises(CursorError) as caught,
        ):
            await _REAL_FETCH()
        self.assertEqual(caught.exception.code, "not_logged_in")
        probe.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
