"""Native registry/API checks with a small provider-manager test double."""

import asyncio
import importlib
import json
import os
import sys
import tempfile
import unittest
from copy import deepcopy
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.core.provider.register import provider_cls_map, provider_registry

from tests.mock_runtime import MockOwner

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
main = importlib.import_module(ROOT.name + ".main")
provider_module = importlib.import_module(ROOT.name + ".cursor_plugin.provider")


class Manager:
    def __init__(self):
        self.oauth = SimpleNamespace(meta=lambda: SimpleNamespace(id="oauth-model"))
        self.inst_map = {"oauth-model": self.oauth}
        self.curr_provider_inst = self.oauth
        self.reloads = []

    async def reload(self, config):
        self.reloads.append(config["id"])
        await self.terminate_provider(config["id"])
        if config["enable"]:
            merged = {"type": "cursor_cli", **config}
            self.inst_map[config["id"]] = provider_module.CursorCLIProvider(merged, {})
            # Match the host selecting a default while creating an instance.
            self.curr_provider_inst = self.inst_map[config["id"]]

    async def terminate_provider(self, pid):
        item = self.inst_map.get(pid)
        if item is self.curr_provider_inst:
            self.curr_provider_inst = None
        if item is not None and hasattr(item, "terminate"):
            await item.terminate()
        self.inst_map.pop(pid, None)


class Config(dict):
    async def save_config_async(self):
        return True


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = Manager()
        self.model = {
            "id": "cursor-model",
            "provider_source_id": "cursor-source",
            "enable": True,
            "model": "mock-plan-model",
        }
        self.config = Config(
            {
                "provider_sources": [
                    {"id": "cursor-source", "type": "cursor_cli"},
                    {"id": "oauth-source", "type": "OpenAI Subscribe"},
                ],
                "provider": [
                    self.model,
                    {"id": "oauth-model", "type": "OpenAI Subscribe", "enable": True},
                ],
            }
        )
        self.context = SimpleNamespace(
            provider_manager=self.manager,
            astrbot_config_mgr=SimpleNamespace(default_conf=self.config),
            registered_web_apis=[],
        )
        self.context.register_web_api = partial(main.Context.register_web_api, self.context)
        self.plugin = main.CursorCLIPlugin(self.context, {"model_id": "mock-plan-model"})
        catalog = patch.object(
            main.runtime,
            "_fetch_available_models",
            new=AsyncMock(return_value=[{"modelId": "mock-plan-model", "name": "Mock Plan"}]),
        )
        catalog.start()
        self.addCleanup(catalog.stop)

    async def asyncTearDown(self):
        for pid, instance in list(self.manager.inst_map.items()):
            if getattr(instance, "_astrbot_cursor_plugin", False):
                await self.manager.terminate_provider(pid)

    async def test_cold_start_leaves_unloaded_models_to_host(self):
        self.manager.inst_map.clear()
        self.manager.curr_provider_inst = None
        await self.plugin.initialize()
        self.assertEqual(self.manager.reloads, [])
        self.assertIsNone(self.manager.curr_provider_inst)

    async def test_selected_provider_and_explicit_override_use_separate_acp_sessions(self):
        catalog = [
            {"modelId": "grok-4.6", "name": "Shared"},
            {"modelId": "grok-4.7", "name": "Shared"},
            {"modelId": "refused-model"},
        ]
        engine_cls = main.runtime.Engine
        self.enterContext(patch.dict(os.environ, {"TEST_CURSOR_CATALOG": json.dumps(catalog)}))
        self.enterContext(
            patch.object(main.runtime, "Engine", partial(engine_cls, owner_factory=MockOwner))
        )
        self.enterContext(
            patch.object(main.runtime, "_fetch_available_models", AsyncMock(return_value=catalog))
        )
        self.model["model"] = "grok-4.6"
        other = {**self.model, "id": "dashboard-model", "model": "grok-4.7"}
        self.config["provider"].append(other)
        plugin = main.CursorCLIPlugin(self.context, {"model_id": "grok-4.6"})
        self.addAsyncCleanup(plugin.terminate)
        await plugin.initialize()
        await self.manager.reload(other)
        first = self.manager.inst_map[self.model["id"]]
        second = self.manager.inst_map[other["id"]]
        self.assertIs(first.engine, second.engine)

        async def chat(provider, session="same", **kwargs):
            return (
                await provider.text_chat(prompt="MOCK_MODEL_ID", session_id=session, **kwargs)
            ).completion_text

        self.manager.curr_provider_inst = first
        original = await chat(self.manager.curr_provider_inst)
        self.assertTrue(original.startswith("grok-4.6/"))
        self.manager.curr_provider_inst = second
        switched = await chat(self.manager.curr_provider_inst)
        self.assertTrue(switched.startswith("grok-4.7/"))
        self.manager.curr_provider_inst = first
        self.assertEqual(await chat(self.manager.curr_provider_inst), original)

        a, b = await asyncio.gather(chat(first, "a"), chat(second, "b"))
        self.assertTrue(a.startswith("grok-4.6/"))
        self.assertTrue(b.startswith("grok-4.7/"))
        overridden = await chat(first, model="cursor-grok-4.7")
        self.assertTrue(overridden.startswith("grok-4.7/"))
        self.assertNotEqual(overridden, switched)
        self.assertNotEqual(await chat(first), original)
        for model, code in (
            ("missing-model", "model_unavailable"),
            ("Shared", "model_ambiguous"),
            ("refused-model", "model_mismatch"),
            ("auto", "model"),
        ):
            with self.subTest(model=model), self.assertRaisesRegex(main.CursorError, code):
                await chat(first, model=model)

        self.manager.curr_provider_inst = second
        await plugin.initialize()
        self.assertIs(self.manager.curr_provider_inst, self.manager.inst_map[other["id"]])
        self.assertTrue((await chat(self.manager.curr_provider_inst)).startswith("grok-4.7/"))
        self.assertEqual(other["model"], "grok-4.7")
        report = await main.runtime.usage_dashboard(7)
        self.assertTrue(
            {"grok-4.6", "grok-4.7"} <= {row["label"] for row in report["groups"]["model"]}
        )

        # The registration seed can disappear from the current account's catalog.
        # The retained, separately registered model must still use ACP normally.
        replacement = main.CursorCLIPlugin(self.context, {"model_id": "removed-seed"})
        self.addAsyncCleanup(replacement.terminate)
        await replacement.initialize()
        self.assertEqual(main.runtime.settings()["model"], "")
        self.assertIs(self.manager.curr_provider_inst, self.manager.inst_map[other["id"]])
        self.assertTrue((await chat(self.manager.curr_provider_inst)).startswith("grok-4.7/"))

    async def test_reload_retained_cursor_and_preserve_oauth_selection(self):
        old = provider_module.CursorCLIProvider({"type": "cursor_cli", **self.model}, {})
        self.manager.inst_map[self.model["id"]] = old
        await self.plugin.initialize()
        self.assertEqual(self.manager.reloads, ["cursor-model"])
        self.assertIsNot(self.manager.inst_map["cursor-model"], old)
        self.assertTrue(old._closed)
        self.assertIs(self.manager.curr_provider_inst, self.manager.oauth)
        self.assertIs(self.manager.inst_map["oauth-model"], self.manager.oauth)

    async def test_module_reload_refreshes_registry_and_selected_instance(self):
        old = provider_module.CursorCLIProvider({"type": "cursor_cli", **self.model}, {})
        self.manager.inst_map[self.model["id"]] = old
        self.manager.curr_provider_inst = old
        provider_module.register_adapter()
        metadata = provider_cls_map["cursor_cli"]
        count = len(provider_registry)
        importlib.reload(provider_module)
        await self.plugin.initialize()
        current = self.manager.inst_map[self.model["id"]]
        self.assertIs(provider_cls_map["cursor_cli"], metadata)
        self.assertIs(metadata.cls_type, provider_module.CursorCLIProvider)
        self.assertEqual(len(provider_registry), count)
        self.assertIsInstance(current, provider_module.CursorCLIProvider)
        self.assertIs(self.manager.curr_provider_inst, current)
        self.assertTrue(old._closed)

    async def test_plugin_exit_does_not_delete_host_owned_models(self):
        await self.plugin.initialize()
        cursor = provider_module.CursorCLIProvider({"type": "cursor_cli", **self.model}, {})
        self.manager.inst_map[self.model["id"]] = cursor
        before = self.manager.inst_map.copy()
        await self.plugin.terminate()
        self.assertEqual(self.manager.inst_map, before)
        self.assertIs(self.manager.curr_provider_inst, self.manager.oauth)
        self.assertFalse(self.plugin._usage_page.active)
        self.assertTrue(cursor.engine.closed)
        with self.assertRaises(main.CursorError) as stopped:
            await cursor.text_chat(prompt="stopped")
        self.assertEqual(stopped.exception.code, "closed")

    async def test_reloading_plugin_replaces_registered_usage_route(self):
        await self.plugin.initialize()
        await self.plugin.terminate()
        replacement = main.CursorCLIPlugin(self.context, {"model_id": "mock-plan-model"})
        self.addAsyncCleanup(replacement.terminate)
        await replacement.initialize()

        ((route, handler, methods, _description),) = self.context.registered_web_apis
        self.assertEqual(route, "/astrbot_plugin_cursor/usage/dashboard")
        self.assertEqual(methods, ["POST"])
        self.assertEqual(handler, replacement._usage_page.handle)

    async def test_failed_cursor_start_keeps_history_available_and_blocks_existing_provider(self):
        await self.plugin.initialize()
        old = provider_module.CursorCLIProvider({"type": "cursor_cli", **self.model}, {})
        self.manager.inst_map[self.model["id"]] = old
        before = deepcopy(self.config)
        with tempfile.TemporaryDirectory() as directory:
            store = main.runtime.UsageStore(directory)
            try:
                store.record({"event": "request_finished", "request_id": "prior", "code": "ok"})
                metrics = importlib.import_module(main.runtime.__package__ + ".metrics")
                with (
                    patch.object(self.context, "_astrbot_cursor_usage_store", store),
                    patch.object(main.runtime, "_usage_store", store),
                    patch.object(metrics, "_usage_store", store),
                ):
                    with patch.object(
                        main.runtime,
                        "_fetch_available_models",
                        AsyncMock(side_effect=main.CursorError("not_logged_in")),
                    ) as fetch:
                        await self.plugin.initialize()
                        report = await main.runtime.usage_dashboard(7)
                        self.assertEqual(report["startup_error"]["code"], "not_logged_in")
                        self.assertFalse(report["validated"])
                        self.assertEqual(report["totals"]["requests"], 1)
                        self.assertTrue(self.plugin._usage_page.active)
                        with self.assertRaises(main.CursorError) as chat:
                            await old.text_chat(prompt="测试")
                        self.assertEqual(chat.exception.code, "closed")
                        with self.assertRaises(main.CursorError) as catalog:
                            await old.get_models()
                        self.assertEqual(catalog.exception.code, "closed")
                        fetch.assert_awaited_once()
                        self.assertEqual(old.engine.workers, [])
                        self.assertEqual(self.config, before)
                        self.assertIs(self.manager.curr_provider_inst, self.manager.oauth)
                    await self.plugin.initialize()
                    report = await main.runtime.usage_dashboard(7)
                    self.assertIsNone(report["startup_error"])
                    self.assertTrue(report["validated"])
                    self.assertEqual(report["totals"]["requests"], 2)
                    self.assertEqual(report["totals"]["failed"], 1)
            finally:
                store.close()

    async def test_missing_model_and_startup_timeout_leave_the_page_registered(self):
        for expected, fetch in (
            ("model_unavailable", AsyncMock(return_value=[{"modelId": "other-plan-model"}])),
            ("startup_timeout", AsyncMock(side_effect=asyncio.TimeoutError())),
        ):
            with self.subTest(expected=expected):
                with patch.object(main.runtime, "_fetch_available_models", fetch):
                    await self.plugin.initialize()
                self.assertTrue(self.plugin._usage_page.active)
                report = await main.runtime.usage_dashboard(7)
                self.assertFalse(report["validated"])
                self.assertEqual(report["startup_error"]["code"], expected)
                with self.assertRaises(main.CursorError) as failed:
                    main.runtime.resolve_model_id("other-plan-model")
                self.assertEqual(failed.exception.code, expected)
                self.assertEqual(self.manager.reloads, [])
        await self.plugin.initialize()

    async def test_oversize_request_records_failure_without_entering_engine(self):
        await self.plugin.initialize()
        cursor = provider_module.CursorCLIProvider({"type": "cursor_cli", **self.model}, {})
        self.addAsyncCleanup(cursor.terminate)
        cursor.settings["context_max_bytes"] = 1024
        with tempfile.TemporaryDirectory() as directory:
            store = main.runtime.UsageStore(directory)
            try:
                metrics = importlib.import_module(main.runtime.__package__ + ".metrics")
                with (
                    patch.object(main.runtime, "_usage_store", store),
                    patch.object(metrics, "_usage_store", store),
                    patch.object(cursor.engine, "submit", new_callable=AsyncMock) as submit,
                ):
                    with self.assertRaises(main.CursorError) as limit:
                        await cursor.text_chat(prompt="x" * 2048, session_id="test-anonymous-label")
                    self.assertEqual(limit.exception.code, "context_limit")
                    report = await main.runtime.usage_dashboard(7)
                submit.assert_not_awaited()
                self.assertEqual(report["totals"]["failed"], 1)
                self.assertEqual(report["totals"]["wire_bytes"], 0)
                self.assertEqual(report["groups"]["model"][0]["label"], "")
                self.assertEqual(report["groups"]["code"][0]["label"], "context_limit")
                self.assertEqual(report["runtime"]["last_failure"], "context_limit")
            finally:
                store.close()
