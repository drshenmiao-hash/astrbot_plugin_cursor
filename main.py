"""AstrBot plugin entry point and corresponding-source notice."""

import asyncio

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .cursor_plugin import runtime
from .cursor_plugin.diagnostics import diagnose
from .cursor_plugin.errors import CursorError
from .cursor_plugin.provider import CursorCLIProvider
from .cursor_plugin.web import UsagePage

__all__ = ["CursorCLIPlugin", "CursorCLIProvider"]


@register("astrbot_plugin_cursor", "drshenmiao-hash", "Cursor CLI 常驻聊天 Provider", "0.4.1")
class CursorCLIPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        runtime.configure(config)
        runtime.configure_usage(context)
        self._usage_page = UsagePage()

    async def initialize(self):
        # Also stops providers retained when upgrading from a version whose teardown only closed the page.
        await runtime.stop_providers(self.context)
        try:
            await runtime.initialize(self.context)
        except CursorError as exc:
            runtime.record_startup_failure(exc.code)
        except asyncio.TimeoutError:
            runtime.record_startup_failure("startup_timeout")
        self._usage_page.register(self.context)

    async def terminate(self):
        self._usage_page.close()
        await runtime.stop_validation()
        await runtime.stop_providers(self.context)

    @filter.command("cursor_source")
    async def cursor_source(self, event: AstrMessageEvent):
        """Show this version's license and corresponding source."""
        yield event.plain_result(
            "astrbot_plugin_cursor v0.4.1 · AGPL-3.0\n"
            "源码：https://github.com/drshenmiao-hash/astrbot_plugin_cursor/tree/v0.4.1"
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
