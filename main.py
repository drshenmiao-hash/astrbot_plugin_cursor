"""AstrBot plugin entry point and corresponding-source notice."""

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .cursor_plugin import runtime
from .cursor_plugin.diagnostics import diagnose
from .cursor_plugin.provider import CursorCLIProvider
from .cursor_plugin.web import UsagePage

__all__ = ["CursorCLIPlugin", "CursorCLIProvider"]


@register("astrbot_plugin_cursor", "drshenmiao-hash", "Cursor CLI 常驻聊天 Provider", "0.3.0")
class CursorCLIPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        runtime.configure(config)
        runtime.configure_usage(context)
        self._usage_page = UsagePage()

    async def initialize(self):
        await runtime.initialize(self.context)
        self._usage_page.register(self.context)

    async def terminate(self):
        self._usage_page.close()

    @filter.command("cursor_source")
    async def cursor_source(self, event: AstrMessageEvent):
        """Show this version's license and corresponding source."""
        yield event.plain_result(
            "astrbot_plugin_cursor v0.3.0 · AGPL-3.0\n"
            "源码：https://github.com/drshenmiao-hash/astrbot_plugin_cursor/tree/v0.3.0"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("cursor_usage")
    async def cursor_usage(self, event: AstrMessageEvent, group: str = "total", key: str = ""):
        """View this plugin's local counts; never queries Cursor billing or balance."""
        yield event.plain_result(await runtime.local_usage(group, key))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("cursor_doctor")
    async def cursor_doctor(self, event: AstrMessageEvent):
        """Check runtime, credentials, SSH and queue health without a model prompt."""
        import json

        result = await diagnose(runtime.settings())
        result["runtime"] = runtime.engine_health()
        yield event.plain_result(json.dumps(result, ensure_ascii=False, indent=2))

    # As in the reference plugin, Provider instances belong to AstrBot's model
    # manager. Plugin unloading does not delete models or change their selection.
