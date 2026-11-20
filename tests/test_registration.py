"""Exercise real native config/save/reload code with explicit host API doubles.

No AstrBot install, CLI, subprocess, network, account or live config is accessed.
ProviderManager and the resulting Provider instance are doubles; tests do not
claim actual AstrBot/WebUI integration coverage.
"""

import sys
import unittest
from copy import deepcopy
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

from cursor_plugin import runtime
from cursor_plugin.errors import CursorError

MODEL = "explicit-plan-model"


class Config(dict):
    def __init__(self, **values):
        super().__init__(values)
        self.saved = []
        self.results = []

    async def save_config_async(self):
        self.saved.append(deepcopy(dict(self)))
        result = self.results.pop(0) if self.results else True
        if isinstance(result, Exception):
            raise result
        return result


class NativeModel:
    def __init__(self, config):
        self.config = deepcopy(config)
        self.closed = False


class Manager:
    def __init__(self, config, *, running=True):
        self.config = config
        self.providers_config = config["provider"]
        self.provider_sources_config = config["provider_sources"]
        self.inst_map = {model["id"]: object() for model in config["provider"]} if running else {}
        self.curr_provider_inst = self.inst_map.get("oauth-model")
        self._mcp_init_task = object() if running else None
        self.reloads = []

    async def reload(self, model):
        self.reloads.append(model["id"])
        source = next(
            (s for s in self.provider_sources_config if s["id"] == model.get("provider_source_id")),
            {},
        )
        merged = {**source, **model}
        if merged.get("type") != "cursor_cli":
            raise AssertionError("Plugin tried to reload an unrelated provider")
        old = self.inst_map.pop(model["id"], None)
        if isinstance(old, NativeModel):
            old.closed = True
        if model.get("enable", True):
            self.inst_map[model["id"]] = NativeModel(merged)
            # Deliberately emulate a host that selects while loading. The plugin
            # must restore the previous selection, including None, afterwards.
            self.curr_provider_inst = self.inst_map[model["id"]]


def existing_config():
    return Config(
        provider_sources=[
            {
                "id": "oauth-source",
                "type": "OpenAI Subscribe",
                "key": "DUMMY_OAUTH",
                "api_base": "https://example.invalid/oauth",
                "enable": True,
                "oauth_accounts": [{"id": "local", "key": "DUMMY_ACCOUNT"}],
                "oauth_routing": {"models": {"oauth-model": "local"}},
            },
            {
                "id": "deepseek-source",
                "type": "openai_chat_completion",
                "key": ["DUMMY_DS"],
                "api_base": "https://example.invalid/deepseek",
                "enable": True,
            },
        ],
        provider=[
            {
                "id": "oauth-model",
                "provider_source_id": "oauth-source",
                "enable": True,
                "model": "oauth-pinned",
                "modalities": ["text", "image", "tool_use"],
            },
            {
                "id": "deepseek-model",
                "provider_source_id": "deepseek-source",
                "enable": True,
                "model": "deepseek-chat",
                "modalities": ["text", "tool_use"],
            },
        ],
        provider_settings={"fallback_chat_models": ["deepseek-model", "oauth-model"]},
        agent={"runtime": "astrbot", "config": {"model": {"provider_id": "oauth-model"}}},
        session_preferences={"group": {"model": "deepseek-model"}},
    )


class RegistrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original_settings = runtime._settings
        self.original_engine = runtime._engine
        self.original_catalog = runtime._available_models
        runtime._engine = None
        runtime.configure({"model_id": MODEL})
        self.catalog_patch = patch.object(
            runtime,
            "_fetch_available_models",
            new=AsyncMock(return_value=[{"modelId": MODEL, "name": "Plan Model"}]),
        )
        self.catalog = self.catalog_patch.start()
        self.addCleanup(self.catalog_patch.stop)
        self.conf = existing_config()
        self.manager = Manager(self.conf)
        self.context = SimpleNamespace(
            astrbot_config_mgr=SimpleNamespace(default_conf=self.conf),
            provider_manager=self.manager,
        )
        provider = ModuleType("cursor_plugin.provider")
        provider.CursorCLIProvider = NativeModel
        self.provider_patch = patch.dict(sys.modules, {"cursor_plugin.provider": provider})
        self.provider_patch.start()
        self.addCleanup(self.provider_patch.stop)
        self.process_patch = patch(
            "asyncio.create_subprocess_exec",
            side_effect=AssertionError("No processes in registration tests"),
        )
        self.process_patch.start()
        self.addCleanup(self.process_patch.stop)

    def tearDown(self):
        runtime._settings = self.original_settings
        runtime._engine = self.original_engine
        runtime._available_models = self.original_catalog

    def cursor_sources(self):
        return [s for s in self.conf["provider_sources"] if s.get("type") == "cursor_cli"]

    def cursor_models(self):
        ids = {s["id"] for s in self.cursor_sources()}
        return [m for m in self.conf["provider"] if m.get("provider_source_id") in ids]

    async def test_first_load_creates_exact_source_and_native_model(self):
        await runtime.initialize(self.context)
        self.assertEqual(len(self.cursor_sources()), 1)
        source = self.cursor_sources()[0]
        self.assertEqual(source["type"], "cursor_cli")
        self.assertEqual(source["key"], "")
        self.assertEqual(source["api_base"], "")
        model = self.cursor_models()[0]
        self.assertEqual(model["id"], MODEL)
        self.assertEqual(model["model"], MODEL)
        self.assertEqual(model["key"], [])
        self.assertEqual(model["provider_source_id"], source["id"])
        self.assertEqual(self.manager.reloads, [MODEL])
        self.assertIn(MODEL, self.manager.inst_map)
        self.assertEqual(len(self.conf.saved), 1)
        self.assertEqual(self.conf.saved[-1], dict(self.conf))

    async def test_repeated_load_does_not_duplicate_or_rewrite_config(self):
        await runtime.initialize(self.context)
        first = self.manager.inst_map[MODEL]
        before = deepcopy(dict(self.conf))
        await runtime.initialize(self.context)
        self.assertEqual(dict(self.conf), before)
        self.assertEqual(len(self.cursor_sources()), 1)
        self.assertEqual(len(self.cursor_models()), 1)
        self.assertEqual(len(self.conf.saved), 1)
        self.assertTrue(first.closed)
        self.assertIsNot(self.manager.inst_map[MODEL], first)
        self.assertEqual(len(self.manager.inst_map), 3)

    async def test_empty_or_auto_does_not_create_model_and_warns(self):
        for value in ["", " ", "auto", "AUTO", " auto ", "auto-smart", "default"]:
            with self.subTest(model_id=value):
                runtime.configure({"model_id": value})
                conf = existing_config()
                manager = Manager(conf)
                context = SimpleNamespace(
                    astrbot_config_mgr=SimpleNamespace(default_conf=conf), provider_manager=manager
                )
                before = deepcopy(conf["provider"])
                with self.assertLogs("astrbot", level="WARNING") as messages:
                    await runtime.initialize(context)
                self.assertIn("model_id", "\n".join(messages.output))
                self.assertIn("未创建原生模型", "\n".join(messages.output))
                self.assertEqual(conf["provider"], before)
                self.assertEqual(manager.reloads, [])
                self.assertEqual(runtime.settings()["model"], "")
                self.assertTrue(any(s["type"] == "cursor_cli" for s in conf["provider_sources"]))

    async def test_default_model_and_fallbacks_remain_unchanged(self):
        selected = self.manager.curr_provider_inst
        fields = {
            key: deepcopy(value)
            for key, value in self.conf.items()
            if key not in {"provider", "provider_sources"}
        }
        await runtime.initialize(self.context)
        self.assertIs(self.manager.curr_provider_inst, selected)
        self.assertEqual({key: self.conf[key] for key in fields}, fields)

    async def test_existing_oauth_and_deepseek_are_untouched(self):
        sources = deepcopy(self.conf["provider_sources"])
        models = deepcopy(self.conf["provider"])
        instances = dict(self.manager.inst_map)
        await runtime.initialize(self.context)
        await runtime.initialize(self.context)
        self.assertEqual(self.conf["provider_sources"][:2], sources)
        self.assertEqual(self.conf["provider"][:2], models)
        for key, instance in instances.items():
            self.assertIs(self.manager.inst_map[key], instance)
        self.assertEqual(self.manager.reloads, [MODEL, MODEL])

    async def test_selected_model_accepts_image_input_without_other_capabilities(self):
        await runtime.initialize(self.context)
        source = self.cursor_sources()[0]
        model = self.cursor_models()[0]
        self.assertEqual(source["modalities"], ["text"])
        self.assertEqual(model["modalities"], ["text", "image"])
        self.assertEqual(self.manager.inst_map[MODEL].config["modalities"], ["text", "image"])
        self.assertEqual(source["key"], "")
        self.assertEqual(model["key"], [])
        self.assertEqual(self.manager.inst_map[MODEL].config["key"], [])
        for config in [source, model, self.manager.inst_map[MODEL].config]:
            self.assertEqual(config["api_base"], "")

    async def test_legacy_cursor_model_keys_are_repaired_and_stay_lists_on_reload(self):
        self.conf["provider_sources"].append(runtime._build_default_source_config())
        self.conf["provider"].extend(
            [
                {
                    "id": MODEL,
                    "model": MODEL,
                    "provider_source_id": "cursor_cli",
                    "type": "cursor_cli",
                    "provider_type": "chat_completion",
                    "enable": True,
                    "key": "",
                    "api_base": "",
                    "modalities": ["text", "image"],
                },
                {
                    "id": "disabled-cursor",
                    "model": "other-model",
                    "type": "cursor_cli",
                    "enable": False,
                    "key": "",
                    "api_base": "",
                    "modalities": ["text"],
                },
            ]
        )

        await runtime.initialize(self.context)
        self.assertEqual([m["key"] for m in self.conf["provider"][-2:]], [[], []])
        self.assertEqual(self.cursor_sources()[0]["key"], "")
        self.assertFalse(self.conf["provider"][-1]["enable"])
        self.assertEqual(self.conf["provider"][-1]["model"], "other-model")
        await runtime.initialize(self.context)
        self.assertEqual([m["key"] for m in self.conf["provider"][-2:]], [[], []])
        self.assertEqual(len(self.conf.saved), 1)

    async def test_failed_model_validation_still_repairs_only_legacy_cursor_keys(self):
        self.conf["provider_sources"].append(runtime._build_default_source_config())
        self.conf["provider"].append(
            {
                "id": MODEL,
                "model": MODEL,
                "provider_source_id": "cursor_cli",
                "key": "",
                "modalities": ["text", "image"],
            }
        )
        expected = deepcopy(dict(self.conf))
        expected["provider"][-1]["key"] = []
        with patch.object(
            runtime,
            "ensure_model_validated",
            new=AsyncMock(side_effect=CursorError("not_logged_in")),
        ):
            with self.assertRaises(CursorError) as error:
                await runtime.initialize(self.context)
        self.assertEqual(error.exception.code, "not_logged_in")
        self.assertEqual(dict(self.conf), expected)
        self.assertEqual(self.conf.saved, [expected])
        self.assertEqual(self.manager.reloads, [])

        self.conf["provider"][-1]["key"] = ""

        async def fail_cleanup(_context):
            self.assertEqual(self.conf["provider"][-1]["key"], [])
            raise CursorError("cleanup_unverified")

        with patch.object(runtime, "stop_providers", side_effect=fail_cleanup):
            with self.assertRaisesRegex(CursorError, "cleanup_unverified"):
                await runtime.initialize(self.context, stop_existing=True)
        self.assertEqual(dict(self.conf), expected)

    async def test_existing_source_and_model_are_updated_in_place(self):
        self.conf["provider_sources"].append(
            {
                "id": "custom-cursor",
                "type": "cursor_cli",
                "enable": True,
                "key": ["OLD"],
                "api_base": "old",
            }
        )
        self.conf["provider"].append(
            {
                "id": MODEL,
                "provider_source_id": "custom-cursor",
                "model": "old-model",
                "enable": True,
                "modalities": ["text", "image", "tool_use"],
                "temperature": 0.4,
                "key": ["OLD"],
            }
        )
        await runtime.initialize(self.context)
        self.assertEqual(len(self.cursor_sources()), 1)
        self.assertEqual(self.cursor_sources()[0]["id"], "custom-cursor")
        self.assertEqual(len(self.cursor_models()), 1)
        self.assertEqual(self.cursor_models()[0]["model"], MODEL)
        self.assertEqual(self.cursor_models()[0]["temperature"], 0.4)
        self.assertEqual(self.cursor_models()[0]["modalities"], ["text", "image"])

    async def test_same_model_alias_is_not_duplicated_or_renamed(self):
        self.conf["provider_sources"].append({"id": "custom-cursor", "type": "cursor_cli"})
        model = {
            "id": "old-cursor-alias",
            "model": MODEL,
            "enable": True,
            "provider_source_id": "custom-cursor",
        }
        self.conf["provider"].append(model)
        old = NativeModel(model)
        self.manager.inst_map[model["id"]] = old
        self.manager.curr_provider_inst = old
        self.conf["agent"]["config"]["model"]["provider_id"] = model["id"]
        await runtime.initialize(self.context)
        self.assertEqual([m["id"] for m in self.cursor_models()], [model["id"]])
        self.assertIs(self.manager.curr_provider_inst, self.manager.inst_map[model["id"]])
        self.assertEqual(self.conf["agent"]["config"]["model"]["provider_id"], model["id"])
        self.assertTrue(old.closed)

    async def test_valid_existing_binding_survives_native_model_id_collision(self):
        self.catalog.return_value = [{"modelId": MODEL}, {"modelId": "another-available-model"}]
        self.conf["provider_sources"].append(runtime._build_default_source_config())
        self.conf["provider"].append(
            {
                "id": MODEL,
                "model": "another-available-model",
                "provider_source_id": "cursor_cli",
                "key": [],
                "api_base": "",
                "modalities": ["text", "image"],
            }
        )
        self.conf["agent"]["config"]["model"]["provider_id"] = MODEL
        self.conf["provider_settings"]["fallback_chat_models"].append(MODEL)
        before = deepcopy(dict(self.conf))
        with self.assertLogs("astrbot", level="WARNING") as messages:
            await runtime.initialize(self.context)

        self.assertIn("保留已有绑定", "\n".join(messages.output))
        self.assertEqual(dict(self.conf), before)
        self.assertEqual(self.conf.saved, [])

    async def test_existing_alias_wins_when_native_id_is_bound_to_another_model(self):
        self.catalog.return_value = [
            {"modelId": MODEL, "name": "Plan Model"},
            {"modelId": "another-available-model"},
        ]
        self.conf["provider_sources"].append(runtime._build_default_source_config())
        bound = {
            "id": MODEL,
            "model": "another-available-model",
            "provider_source_id": "cursor_cli",
            "enable": False,
            "key": [],
            "modalities": ["text", "image"],
        }
        self.conf["provider"].extend(
            [
                bound,
                {"id": "selected-model-alias", "model": "Plan Model", "type": "cursor_cli"},
            ]
        )

        await runtime.initialize(self.context)

        self.assertEqual(self.conf["provider"][-2], bound)
        alias = self.conf["provider"][-1]
        self.assertEqual((alias["id"], alias["model"]), ("selected-model-alias", MODEL))
        self.assertEqual(self.manager.reloads, ["selected-model-alias"])

    async def test_disabled_source_stays_disabled_and_new_model_is_disabled(self):
        self.conf["provider_sources"].append(
            {"id": "custom-cursor", "type": "cursor_cli", "enable": False}
        )
        await runtime.initialize(self.context)
        self.assertFalse(self.cursor_sources()[0]["enable"])
        self.assertFalse(self.cursor_models()[0]["enable"])
        self.assertEqual(self.manager.reloads, [])

    async def test_foreign_model_id_collision_never_overwrites(self):
        self.conf["provider"][0]["id"] = MODEL
        before = deepcopy(self.conf["provider"])
        with self.assertLogs("astrbot", level="WARNING") as messages:
            await runtime.initialize(self.context)
        self.assertIn("其他提供商占用", "\n".join(messages.output))
        self.assertEqual(self.conf["provider"], before)
        self.assertEqual(self.cursor_models(), [])
        self.assertEqual(self.manager.reloads, [])

    async def test_explicit_foreign_type_on_cursor_source_blocks_matching_model_id(self):
        self.conf["provider_sources"].append(runtime._build_default_source_config())
        self.conf["provider"].append(
            {
                "id": MODEL,
                "model": MODEL,
                "provider_source_id": "cursor_cli",
                "type": "openai_chat_completion",
                "enable": True,
                "key": ["DUMMY_FOREIGN"],
                "api_base": "https://example.invalid/foreign",
                "modalities": ["text", "tool_use"],
            }
        )
        before = deepcopy(dict(self.conf))
        instance = self.manager.inst_map[MODEL] = object()

        with self.assertLogs("astrbot", level="WARNING") as messages:
            await runtime.initialize(self.context)

        self.assertIn("其他提供商占用", "\n".join(messages.output))
        self.assertEqual(dict(self.conf), before)
        self.assertEqual(self.conf.saved, [])
        self.assertEqual(self.manager.reloads, [])
        self.assertIs(self.manager.inst_map[MODEL], instance)

    async def test_legacy_key_repair_skips_explicit_foreign_types(self):
        self.conf["provider_sources"].append(runtime._build_default_source_config())
        self.conf["provider"].extend(
            [
                {"id": "inherited-cursor", "provider_source_id": "cursor_cli", "key": ""},
                {
                    "id": "foreign-on-cursor-source",
                    "provider_source_id": "cursor_cli",
                    "type": "openai_chat_completion",
                    "key": "",
                },
            ]
        )
        expected = deepcopy(dict(self.conf))
        expected["provider"][-2]["key"] = []

        await runtime._repair_legacy_model_keys(self.conf)

        self.assertEqual(dict(self.conf), expected)
        self.assertEqual(self.conf.saved, [expected])

    async def test_foreign_source_id_collision_never_overwrites(self):
        self.conf["provider_sources"][0]["id"] = "cursor_cli"
        before = deepcopy(dict(self.conf))
        with self.assertRaises(CursorError) as error:
            await runtime.initialize(self.context)
        self.assertEqual(error.exception.code, "source_id_conflict")
        self.assertEqual(dict(self.conf), before)
        self.assertEqual(self.conf.saved, [])

    async def test_cold_start_syncs_config_and_defers_instances_to_host(self):
        self.manager = Manager(self.conf, running=False)
        self.context.provider_manager = self.manager
        await runtime.initialize(self.context)
        self.assertEqual(len(self.cursor_models()), 1)
        self.assertIs(self.manager.providers_config, self.conf["provider"])
        self.assertIs(self.manager.provider_sources_config, self.conf["provider_sources"])
        self.assertEqual(self.manager.inst_map, {})
        self.assertEqual(self.manager.reloads, [])
        self.assertIsNone(self.manager.curr_provider_inst)

    async def test_running_empty_manager_loads_model_without_selecting_it(self):
        self.conf["provider"] = []
        self.manager = Manager(self.conf)
        self.context.provider_manager = self.manager
        self.assertIsNone(self.manager.curr_provider_inst)
        await runtime.initialize(self.context)
        self.assertIn(MODEL, self.manager.inst_map)
        self.assertIsNone(self.manager.curr_provider_inst)

    async def test_save_failure_rolls_back_only_registration(self):
        before = deepcopy(dict(self.conf))
        selected = self.manager.curr_provider_inst
        self.conf.results = [OSError("simulated write failure")]
        with self.assertRaises(CursorError) as error:
            await runtime.initialize(self.context)
        self.assertEqual(error.exception.code, "registration_save_failed")
        self.assertEqual(dict(self.conf), before)
        self.assertEqual(self.manager.reloads, [])
        self.assertIs(self.manager.curr_provider_inst, selected)

    async def test_superseded_save_is_retried_once(self):
        self.conf.results = [False, True]
        await runtime.initialize(self.context)
        self.assertEqual(len(self.conf.saved), 2)
        self.assertEqual(len(self.cursor_models()), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
