"""AstrBot plugin entry point. No commands, HTTP endpoints or core patches."""

from astrbot.api import AstrBotConfig
from astrbot.api.star import Context, Star, register

from .cursor_plugin import runtime
from .cursor_plugin.errors import CursorError
from .cursor_plugin.provider import CursorCLIProvider

__all__ = ["CursorCLIPlugin", "CursorCLIProvider"]
_RELOAD_STATE = "_astrbot_cursor_cli_reload_state"


@register("astrbot_plugin_cursor", "drshenmiao-hash", "Cursor CLI 原生聊天 Provider", "0.1.0")
class CursorCLIPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        runtime.configure(config)

    async def initialize(self):
        manager = self.context.provider_manager
        state = getattr(self.context, _RELOAD_STATE, None)
        if state is None:
            # During cold startup AstrBot loads all configured providers after
            # plugin initialization. A new install uses the native Add Source UI.
            setattr(self.context, _RELOAD_STATE, {})
            return
        config = self.context.astrbot_config_mgr.default_conf
        source_ids = {
            s["id"]
            for s in config.get("provider_sources", [])
            if s.get("type") == runtime.PROVIDER_TYPE
        }
        own = [
            m
            for m in config.get("provider", [])
            if m.get("type") == runtime.PROVIDER_TYPE or m.get("provider_source_id") in source_ids
        ]
        selected = manager.curr_provider_inst
        selected_id = selected.meta().id if selected is not None else state.get("selected_id")
        try:
            # Only reload Cursor instances. No source migrations, global metadata
            # edits, OpenAI aliases, or writes to another plugin's configuration.
            for model in own:
                await manager.reload(model)
                if model.get("enable", True) and not isinstance(
                    manager.inst_map.get(model["id"]), CursorCLIProvider
                ):
                    raise CursorError("config")
        finally:
            if selected_id in manager.inst_map:
                manager.curr_provider_inst = manager.inst_map[selected_id]
            setattr(self.context, _RELOAD_STATE, {})

    async def terminate(self):
        manager = self.context.provider_manager
        selected = manager.curr_provider_inst
        state = {}
        if selected is not None and getattr(selected, "_astrbot_cursor_plugin", False):
            state["selected_id"] = selected.meta().id
        setattr(self.context, _RELOAD_STATE, state)
        for provider_id, instance in list(manager.inst_map.items()):
            if getattr(instance, "_astrbot_cursor_plugin", False):
                await manager.terminate_provider(provider_id)
        runtime.suspend()
        # Keep the type entry so stale references fail with a safe 'closed'
        # error. The next import replaces cls_type, as in OpenAI OAuth's pattern.
