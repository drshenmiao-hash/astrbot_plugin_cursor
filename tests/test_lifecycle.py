"""Uses real AstrBot registry/API with a small provider-manager test double."""

import importlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
main = importlib.import_module(ROOT.name + ".main")
# main uses relative package imports, so configure that same module instance.
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
            self.inst_map[config["id"]] = provider_module.CursorCLIProvider(config, {})

    async def terminate_provider(self, pid):
        item = self.inst_map.pop(pid, None)
        if item is not None and hasattr(item, "terminate"):
            await item.terminate()
        if item is self.curr_provider_inst:
            self.curr_provider_inst = None


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_reload_and_oauth_coexistence(self):
        manager = Manager()
        config = {
            "provider_sources": [
                {"id": "cursor-source", "type": "cursor_cli"},
                {"id": "oauth-source", "type": "OpenAI Subscribe"},
            ],
            "provider": [
                {
                    "id": "cursor-model",
                    "type": "cursor_cli",
                    "provider_source_id": "cursor-source",
                    "enable": True,
                    "model": "mock-plan-model",
                },
                {"id": "oauth-model", "type": "OpenAI Subscribe", "enable": True},
            ],
        }
        context = SimpleNamespace(
            provider_manager=manager, astrbot_config_mgr=SimpleNamespace(default_conf=config)
        )
        # Star's base only stores context; no network or model calls occur.
        plugin = main.CursorCLIPlugin(context, {"model_id": "mock-plan-model"})
        await plugin.initialize()
        self.assertEqual(manager.reloads, [])  # Cold startup leaves native loading to AstrBot.
        manager.inst_map["cursor-model"] = provider_module.CursorCLIProvider(
            config["provider"][0], {}
        )
        manager.curr_provider_inst = manager.inst_map["cursor-model"]
        await plugin.terminate()
        self.assertIs(manager.inst_map["oauth-model"], manager.oauth)
        plugin2 = main.CursorCLIPlugin(context, {"model_id": "mock-plan-model"})
        await plugin2.initialize()
        self.assertEqual(manager.reloads, ["cursor-model"])
        self.assertIsInstance(manager.curr_provider_inst, provider_module.CursorCLIProvider)
        self.assertIs(manager.inst_map["oauth-model"], manager.oauth)
        await plugin2.terminate()
