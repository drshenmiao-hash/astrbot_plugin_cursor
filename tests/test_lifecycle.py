"""Native registry/API checks with a small provider-manager test double."""

import importlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

from astrbot.core.provider.register import provider_cls_map, provider_registry

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


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = Manager()
        self.model = {
            "id": "cursor-model",
            "provider_source_id": "cursor-source",
            "enable": True,
            "model": "mock-plan-model",
        }
        self.config = {
            "provider_sources": [
                {"id": "cursor-source", "type": "cursor_cli"},
                {"id": "oauth-source", "type": "OpenAI Subscribe"},
            ],
            "provider": [
                self.model,
                {"id": "oauth-model", "type": "OpenAI Subscribe", "enable": True},
            ],
        }
        self.context = SimpleNamespace(
            provider_manager=self.manager,
            astrbot_config_mgr=SimpleNamespace(default_conf=self.config),
        )
        self.plugin = main.CursorCLIPlugin(self.context, {"model_id": "mock-plan-model"})

    async def asyncTearDown(self):
        for pid, instance in list(self.manager.inst_map.items()):
            if getattr(instance, "_astrbot_cursor_plugin", False):
                await self.manager.terminate_provider(pid)

    async def test_cold_start_leaves_unloaded_models_to_host(self):
        await self.plugin.initialize()
        self.assertEqual(self.manager.reloads, [])
        self.assertIs(self.manager.curr_provider_inst, self.manager.oauth)

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
        # AstrBot calls plugin teardown only when explicitly defined on the class.
        # Like the reference plugin, no teardown here removes Provider instances.
        self.assertNotIn("terminate", main.CursorCLIPlugin.__dict__)
        self.assertFalse(hasattr(self.context, "_astrbot_cursor_cli_reload_state"))
