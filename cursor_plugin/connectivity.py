"""Redacted Plugin Views diagnostics over the same isolated CLI transport."""

import asyncio
import time

from .diagnostics import diagnose
from .engine import Engine
from .errors import MESSAGES, CursorError
from .request_status import safe_code, safe_model_id, safe_transport

_START_TIMEOUT = 20
_CHECK_TIMEOUT = 25
_TEST_TIMEOUT = 20
_CLEANUP_ERRORS = {"cleanup_unverified", "cleanup_timeout", "runner_cleanup_unsupported"}


def _step(status, reason):
    return {"status": status, "reason": reason}


def _reason(code):
    return MESSAGES.get(safe_code(code), "检查失败，原始输出已隐藏。")


def _static_steps(report):
    mode = report.get("transport")
    if mode == "local":
        runner_keys = ("file_isolation", "cli_installation_or_sandbox")
        cli_installation = "cli_installation"
        auth = report.get("login_file")
    else:
        runner_keys = (
            "ssh_configuration",
            "ssh_connection",
            "runner_protocol",
            "image_sha",
            "runner_cleanup_protocol",
        )
        cli_installation = "cli_executable"
        auth = report.get("login_volume")
    runner_error = next(
        (
            report[key]
            for key in runner_keys
            if report.get(key) in MESSAGES and report[key] not in {"cli_missing", "cli_layout"}
        ),
        None,
    )
    cli_error = report.get(cli_installation) if report.get(cli_installation) in MESSAGES else None
    if report.get("cli_installation_or_sandbox") in {"cli_missing", "cli_layout"}:
        cli_error = report["cli_installation_or_sandbox"]
    runner_ok = (
        report.get("file_isolation") == "ok"
        if mode == "local"
        else all(
            report.get(key) == "ok"
            for key in ("ssh_connection", "image_sha", "runner_cleanup_protocol")
        )
    )

    if runner_error:
        runner = _step("failed", _reason(runner_error))
    elif runner_ok:
        runner = _step(
            "passed", "本地隔离可用。" if mode == "local" else "SSH 运行器已响应，清理协议可用。"
        )
    else:
        runner = _step("skipped", "依赖检查未确认运行器可用。")
    if cli_error:
        cli = _step("failed", _reason(cli_error))
    elif report.get(cli_installation) == "ok" and report.get("cli_version") not in {
        None,
        "unknown",
    }:
        cli = _step("passed", "Cursor CLI 版本检查通过。")
    else:
        cli = _step("unknown", "未确认 Cursor CLI 能正常执行。")
    login = (
        _step("failed", _reason(auth))
        if auth in MESSAGES
        else _step("unknown", "已找到本地登录材料，登录有效性待确认。")
        if auth == "ok"
        else _step("unknown", "未确认 Cursor 登录状态。")
    )
    return {"runner": runner, "cli": cli, "auth": login}


async def _close(probe):
    # HTTP disconnects and plugin reloads must finish killing only this probe.
    closing = asyncio.create_task(probe.close())
    try:
        await asyncio.shield(closing)
    except asyncio.CancelledError:
        await asyncio.gather(closing, return_exceptions=True)
        raise


async def _probe(cfg, *, test=False):
    probe = Engine(cfg)
    transport = model_id = error = cleanup_error = None
    stage = "runner"
    session_id = None
    handshake = False
    turn = {}
    started = time.monotonic()
    try:
        await probe._new_client(startup_timeout=_START_TIMEOUT)
        transport = safe_transport(probe.owner.kind)
        handshake = True
        if test:
            stage = "model"
            client = probe.client
            async with asyncio.timeout(min(_TEST_TIMEOUT, cfg["timeout_seconds"])):
                model = cfg["model"]
                session_id = await client.new_session(
                    model or cfg["requested_model"], resolve_alias=not model
                )
                model_id = safe_model_id(client.session_models.get(session_id))
                await client.prompt(session_id, "Reply with OK.", stats=turn)
    except CursorError as exc:
        error = exc.code
        stage = exc.stage or ("auth" if error == "not_logged_in" else stage)
        transport = exc.transport or transport
        handshake = handshake or exc.handshake_complete
    except TimeoutError:
        error = "timeout"
    except Exception:
        error = "cli_failed"
    finally:
        try:
            if (
                turn.get("delivery") in {"sent", "unconfirmed"}
                and session_id
                and (error == "timeout" or asyncio.current_task().cancelling())
            ):
                try:
                    await asyncio.wait_for(
                        probe.client.notify("session/cancel", {"sessionId": session_id}), 1
                    )
                except Exception:
                    # The owned connection is closed even if cancel cannot reach it.
                    pass
        finally:
            try:
                await _close(probe)
            except CursorError as exc:
                cleanup_error = exc.code
            except Exception:
                cleanup_error = "cleanup_unverified"
    return {
        "code": safe_code(error or "ok"),
        "stage": stage,
        "handshake": handshake,
        "cleanup": safe_code(cleanup_error) if cleanup_error else None,
        "model_id": model_id,
        "transport": safe_transport(transport),
        "duration_ms": round((time.monotonic() - started) * 1000),
    }


def _auth_acknowledged():
    return _step("unknown", "ACP 认证接口已应答，账号实际可用性需通过模型请求确认。")


async def check_connection(cfg, *, live=None):
    try:
        async with asyncio.timeout(_CHECK_TIMEOUT):
            report = await diagnose(cfg)
    except (TimeoutError, CursorError) as exc:
        report = {"transport": cfg["transport"]}
        dependency_error = _step(
            "failed", _reason(exc.code) if isinstance(exc, CursorError) else "依赖检查超时。"
        )
    else:
        dependency_error = None

    steps = _static_steps(report)
    if dependency_error:
        steps["runner"] = dependency_error
    steps["acp"] = _step("skipped", "运行器或登录材料未就绪，未发起 ACP 握手。")
    transport = safe_transport(report["transport"])
    if live and live["handshake"]:
        transport = live["transport"] or transport
        steps["acp"] = _step("passed", "读取现有连接的 ACP 握手结果。")
        if live["authenticated"]:
            steps["auth"] = _auth_acknowledged()
    elif (
        steps["runner"]["status"] == "passed"
        and steps["cli"]["status"] != "failed"
        and steps["auth"]["status"] != "failed"
    ):
        result = await _probe(cfg)
        transport = result["transport"] or transport
        code = result["code"]
        if code == "runner_in_use":
            steps["acp"] = _step("skipped", "运行器已被占用，未启动诊断连接。")
        elif result["handshake"]:
            steps["runner"] = _step("passed", "诊断运行器已启动。")
            steps["cli"] = _step("passed", "Cursor CLI 已响应。")
            steps["acp"] = _step("passed", "ACP v1 握手通过。")
            steps["auth"] = _auth_acknowledged() if code == "ok" else _step("failed", _reason(code))
        elif result["stage"] == "auth":
            steps["auth"] = _step("failed", _reason(code))
        elif result["stage"] == "handshake":
            steps["acp"] = _step("failed", _reason(code))
        else:
            steps["runner"] = _step("failed", _reason(code))
        if failure := result["cleanup"] or (code if code in _CLEANUP_ERRORS else None):
            steps["cleanup"] = _step("failed", _reason(failure))
    return {"transport": transport, "steps": steps}


async def test_model(cfg, *, live=None):
    if not (cfg["model"] or cfg["requested_model"]):
        return {"status": "skipped", "code": "model", "reason": _reason("model")}
    if live:
        return {
            "status": "skipped",
            "code": "runner_in_use",
            "reason": "聊天运行器正在使用。为保留会话，本次未启动测试。",
            "transport": live["transport"],
        }
    result = await _probe(cfg, test=True)
    code = result["code"]
    status = "passed" if code == "ok" else "skipped" if code == "runner_in_use" else "failed"
    reason = (
        "模型已返回文本。"
        if code == "ok"
        else "测试超过等待时间，诊断连接已结束。"
        if code == "timeout" and not result["cleanup"]
        else "测试超过等待时间。"
        if code == "timeout"
        else _reason(code)
    )
    if result["cleanup"]:
        status = "failed"
        reason += " " + _reason(result["cleanup"])
    return {**result, "status": status, "reason": reason}
