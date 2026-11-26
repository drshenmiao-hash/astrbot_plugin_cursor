"""Redacted Plugin Views diagnostics over the same isolated CLI transport."""

import asyncio
import time

from .diagnostics import diagnose, safe_cli_version
from .engine import Engine
from .errors import MESSAGES, CursorError
from .request_status import safe_code, safe_model_id, safe_transport
from .settings import LOCAL_TRANSPORTS

_START_TIMEOUT = 20
_CHECK_TIMEOUT = 25
_TEST_TIMEOUT = 20
_CLEANUP_ERRORS = {"cleanup_unverified", "cleanup_timeout", "runner_cleanup_unsupported"}


def _step(status, reason):
    return {"status": status, "reason": reason}


def _reason(code):
    return MESSAGES.get(safe_code(code), "检查失败，原始输出已隐藏。")


def _dependency(code, passed):
    if code == "ok":
        return _step("passed", passed)
    if code in MESSAGES:
        return _step("failed", _reason(code))
    return _step("unknown", "待检查。")


def _static_steps(report):
    mode = report["transport"]
    local = mode in LOCAL_TRANSPORTS
    cli_code = report.get("cli_installation" if local else "cli_executable")
    version = safe_cli_version(report.get("cli_version"))
    if cli_code == "ok" and not version:
        cli = _step("unknown", "已检查执行权限，版本未验证。")
    else:
        cli = _dependency(cli_code, f"版本 {version}。")

    if local:
        sandbox = report.get("file_isolation")
        runner = (
            _dependency(report.get("local_runtime", "ok"), "临时工作目录可用。")
            if mode == "local_direct"
            else _dependency(sandbox, "bubblewrap 已实际创建沙盒。")
        )
        steps = {
            "bubblewrap": _step("disabled", "管理员选择了直接运行。")
            if mode == "local_direct"
            else _dependency(report.get("bubblewrap"), "已找到 bubblewrap。"),
        }
        login = report.get("login_file")
    else:
        config_error = report.get("ssh_configuration")
        ssh = report.get("ssh_connection")
        ssh_error = config_error or (ssh if ssh in MESSAGES else None)
        prerequisites = (
            report.get("runner_protocol"),
            report.get("image_sha"),
            report.get("runner_cleanup_protocol"),
        )
        runner = _dependency(
            ssh_error
            or next((code for code in prerequisites if code in MESSAGES), None)
            or ("ok" if ssh == "ok" and prerequisites[1:] == ("ok", "ok") else None),
            "SSH 专用 Runner 已响应，入口校验和清理协议通过。",
        )
        steps = {
            "ssh": _dependency(ssh_error or ssh, "已通过客户端密钥和固定主机公钥连接。"),
            "ssh_key": _dependency(
                config_error
                if config_error in {"ssh_key_missing", "ssh_key_permissions"}
                else ssh
                if ssh == "ssh_auth_denied"
                else report.get("client_key"),
                "密钥文件及权限检查通过。",
            ),
            "host_key": _dependency(
                config_error
                if config_error == "ssh_host_key_missing"
                else ssh
                if ssh == "ssh_host_key_mismatch"
                else "ok"
                if ssh == "ok"
                else None,
                "Runner 主机身份已验证。",
            ),
            "runner_cleanup": _dependency(
                report.get("runner_cleanup_protocol"), "Runner 支持退出清理回执。"
            ),
        }
        login = report.get("login_volume")
    auth = (
        _step("unknown", "已找到登录材料。用「检查连接」验证 ACP 认证。")
        if login == "ok"
        else _dependency(login, "")
    )
    return {**steps, "runner": runner, "cli": cli, "auth": auth}


async def _close(probe):
    # HTTP disconnects and plugin reloads must finish killing only this probe.
    closing = asyncio.create_task(probe.close())
    try:
        await asyncio.shield(closing)
    except asyncio.CancelledError:
        await asyncio.gather(closing, return_exceptions=True)
        raise


async def _probe(cfg, *, test=False, validate=False):
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
        if test or validate:
            stage = "model"
            client = probe.client
            async with asyncio.timeout(min(_TEST_TIMEOUT, cfg["timeout_seconds"])):
                model = cfg["model"]
                session_id = await client.new_session(
                    model or cfg["requested_model"], resolve_alias=not model
                )
                model_id = safe_model_id(client.session_models.get(session_id))
                if test:
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


async def check_environment(cfg):
    try:
        async with asyncio.timeout(_CHECK_TIMEOUT):
            report = await diagnose(cfg)
    except (TimeoutError, CursorError, OSError) as exc:
        report = {"transport": cfg["transport"]}
        dependency_error = _step(
            "failed",
            _reason(exc.code)
            if isinstance(exc, CursorError)
            else "环境检查未完成，请检查路径及权限。",
        )
    else:
        dependency_error = None

    steps = _static_steps(report)
    if dependency_error:
        steps["runner"] = dependency_error
    if steps["runner"]["status"] == steps["cli"]["status"] == "passed":
        steps["environment"] = _step("passed", "运行端与 CLI 版本检查通过。")
    else:
        steps["environment"] = _step("unknown", "请处理运行端或 CLI 检查结果。")
    steps["model"] = (
        _step("passed", f"已校验 {cfg['model']}。")
        if cfg["model"]
        else _step("unknown", "模型已填写，等待 ACP 校验。")
        if cfg["requested_model"]
        else _step("skipped", "请在插件设置填入明确的模型 ID 并保存。")
    )
    transport = safe_transport(report["transport"])
    automatic = cfg["transport"] == "auto" and transport == "ssh"
    return {
        "transport": transport,
        "configured_transport": cfg["transport"],
        "cli_path": report.get("cli_path") if transport in LOCAL_TRANSPORTS or automatic else None,
        "cli_resolved_path": report.get("cli_resolved_path")
        if transport in LOCAL_TRANSPORTS
        else None,
        "cli_state": report.get("cli_state") if transport in LOCAL_TRANSPORTS else None,
        "cli_version": safe_cli_version(report.get("cli_version")),
        "root_warning": report.get("root_warning", False),
        "auto_reason": (
            _reason(report["cli_installation"])
            if automatic and report.get("cli_installation") in MESSAGES
            else _reason("sandbox_missing")
            if automatic
            else None
        ),
        "steps": steps,
    }


async def check_connection(cfg, *, live=None):
    report = await check_environment(cfg)
    steps = report["steps"]
    steps["acp"] = _step("skipped", "运行端或登录材料未就绪。")
    if callable(live):
        live = live()
    auth_acknowledged = False
    if live and live["handshake"]:
        report["transport"] = live["transport"] or report["transport"]
        steps["acp"] = _step("passed", "读取现有连接的 ACP 握手结果。")
        if live["authenticated"]:
            auth_acknowledged = True
            steps["auth"] = _auth_acknowledged()
    elif live:
        steps["acp"] = _step("skipped", "聊天运行端正在启动，本次未建立诊断连接。")
    elif (
        steps["runner"]["status"] == "passed"
        and steps["cli"]["status"] != "failed"
        and steps["auth"]["status"] != "failed"
    ):
        result = await _probe(cfg, validate=bool(cfg["requested_model"]))
        report["transport"] = result["transport"] or report["transport"]
        code = result["code"]
        if code == "runner_in_use":
            steps["acp"] = _step("skipped", "运行端已被占用，本次未建立诊断连接。")
        elif result["handshake"]:
            steps["acp"] = _step("passed", "ACP v1 握手通过。")
            auth_acknowledged = code == "ok" or result["stage"] == "model"
            steps["auth"] = (
                _auth_acknowledged() if auth_acknowledged else _step("failed", _reason(code))
            )
            if result["stage"] == "model":
                steps["model"] = (
                    _step("passed", f"ACP 已确认 {result['model_id']}。")
                    if code == "ok"
                    else _step("failed", _reason(code))
                )
        elif result["stage"] == "auth":
            steps["auth"] = _step("failed", _reason(code))
        elif result["stage"] == "handshake":
            steps["acp"] = _step("failed", _reason(code))
        else:
            steps["runner"] = _step("failed", _reason(code))
        if failure := result["cleanup"] or (code if code in _CLEANUP_ERRORS else None):
            steps["cleanup"] = _step("failed", _reason(failure))
    if auth_acknowledged and steps["model"]["status"] == "passed" and "cleanup" not in steps:
        steps["ready"] = (
            _step("passed", "已满足基本连接条件。请在 AstrBot 选择已启用的 Cursor 模型开始聊天。")
            if cfg["model"]
            else _step("unknown", "请重载插件以完成模型注册，再选择该模型。")
        )
    else:
        steps["ready"] = _step("unknown", "请根据上方结果完成登录和模型校验，再检查连接。")
    return report


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
