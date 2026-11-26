"""Usage and administrator diagnostics via AstrBot's authenticated page bridge."""

import asyncio

from astrbot.api import logger

from .connectivity import check_connection, check_environment, test_model
from .runtime import live_connection, settings, usage_dashboard
from .settings import TRANSPORTS
from .usage import DASHBOARD_RANGES

ROUTE = "/astrbot_plugin_cursor/usage/dashboard"
_NO_CACHE = {"Cache-Control": "no-store"}


class UsagePage:
    def __init__(self):
        self.active = False
        self._context = None
        self._diagnostic_task = None

    def register(self, context):
        self._context = context
        context.register_web_api(ROUTE, self.handle, ["POST"], "Cursor 用量与运行状态")
        self.active = True

    def close(self):
        self.active = False
        context, self._context = self._context, None
        apis = getattr(context, "registered_web_apis", None)
        if isinstance(apis, list):
            # AstrBot 4.29/4.30 replace routes on registration and expose no
            # unregister method. Remove only this instance's handler.
            apis[:] = [api for api in apis if not (api[0] == ROUTE and api[1] == self.handle)]

    async def stop_diagnostics(self):
        task = self._diagnostic_task
        if task is not None:
            if not task.done() and not task.cancelling():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _diagnose(self, action, preview=None):
        cfg = settings()
        if action == "environment":
            if preview is not None and preview != cfg["transport"]:
                cfg.update(transport=preview, model="")
            return await check_environment(cfg)
        if action == "check":
            return await check_connection(cfg, live=lambda: live_connection(cfg))
        return await test_model(cfg, live=live_connection(cfg))

    async def handle(self):
        from astrbot.api.web import json_response, request
        from astrbot.core.workspace import API_KEY_USERNAME_PREFIX

        def error(message, status_code):
            return json_response(
                {"status": "error", "message": message},
                status_code=status_code,
                headers=_NO_CACHE,
            )

        username = request.username
        if not username or username.startswith(API_KEY_USERNAME_PREFIX):
            return error("请登录 AstrBot 管理面板后查看。", 403)
        if not self.active:
            return error("Cursor 插件当前已停止。", 503)
        body = await request.json(default=None)
        if not self.active:
            return error("Cursor 插件当前已停止。", 503)
        if isinstance(body, dict) and "action" in body:
            admin = self._context.get_config().get("dashboard", {}).get("username")
            if username != admin:
                return error("只有 AstrBot 管理员可以执行诊断。", 403)
            action = body["action"]
            preview = body.get("transport")
            valid = action in ("environment", "check", "test") and (
                set(body) == {"action"}
                or (
                    action == "environment"
                    and set(body) == {"action", "transport"}
                    and preview in TRANSPORTS
                )
            )
            if not valid:
                return error("诊断参数无效。", 400)
            if self._diagnostic_task is not None:
                return error("已有诊断正在执行，请稍后再试。", 409)
            task = asyncio.create_task(
                self._diagnose(action, preview) if preview is not None else self._diagnose(action)
            )
            self._diagnostic_task = task
            try:
                result = await asyncio.shield(task)
            except asyncio.CancelledError:
                if not self.active and not asyncio.current_task().cancelling():
                    return error("Cursor 插件当前已停止。", 503)
                if not task.done() and not task.cancelling():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
            except Exception:
                logger.warning("cursor_diagnostic_web code=diagnostic_failed action=%s", action)
                return error("诊断未完成，请稍后重试。", 503)
            finally:
                self._diagnostic_task = None
            return json_response({"status": "success", "data": result}, headers=_NO_CACHE)

        days = body.get("days") if isinstance(body, dict) else None
        if type(days) is not int or days not in DASHBOARD_RANGES:
            return error("请选择近 7、30 或 90 天。", 400)
        try:
            report = await usage_dashboard(days)
        except Exception:
            logger.warning("cursor_usage_web code=read_failed")
            return error("读取本地统计失败，请检查插件数据目录后重试。", 503)
        return json_response({"status": "success", "data": report}, headers=_NO_CACHE)
