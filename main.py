"""AstrBot plugin entry point and corresponding-source notice."""

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .cursor_plugin import runtime
from .cursor_plugin.provider import CursorCLIProvider

__all__ = ["CursorCLIPlugin", "CursorCLIProvider"]


@register("astrbot_plugin_cursor", "drshenmiao-hash", "Cursor CLI 原生聊天 Provider", "0.1.1")
class CursorCLIPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        runtime.configure(config)

    async def initialize(self):
        await runtime.initialize(self.context)

    @filter.command("cursor_source")
    async def cursor_source(self, event: AstrMessageEvent):
        """Show this version's license and corresponding source."""
        yield event.plain_result(
            "astrbot_plugin_cursor v0.1.1 · AGPL-3.0\n"
            "源码：https://github.com/drshenmiao-hash/astrbot_plugin_cursor/tree/v0.1.1"
        )

    # As in the reference plugin, Provider instances belong to AstrBot's model
    # manager. Plugin unloading does not delete models or change their selection.
