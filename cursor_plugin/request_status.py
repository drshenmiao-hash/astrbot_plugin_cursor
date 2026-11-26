"""Display only confirmed request stages and allowlisted metadata."""

from .errors import MESSAGES
from .protocol import MODEL_PATTERN
from .settings import LOCAL_TRANSPORTS

_STAGES = {
    "preflight": "插件预检拒绝",
    "queue": "请求未开始",
    "runner": "Runner 或 CLI 启动失败",
    "handshake": "ACP 握手失败",
    "auth": "认证失败",
    "model": "模型请求失败",
    "unknown": "失败阶段未记录",
}
_PREFLIGHT_CODES = {
    "image_limit",
    "image_invalid",
    "image_source",
    "image_unsupported",
    "context_limit",
    "empty_input",
    "model",
    "model_not_validated",
    "unsupported_image",
    "unsupported_audio",
    "unsupported_tool_calls",
    "unsupported_content",
}
_IMAGE_LIMITS = {
    "count": "张数",
    "file_bytes": "单张大小",
    "total_bytes": "总大小",
    "pixels": "像素数",
    "frame_bytes": "ACP 帧大小",
}


def safe_code(code):
    return (
        code
        if isinstance(code, str) and (code in MESSAGES or code in _PREFLIGHT_CODES or code == "ok")
        else "unknown"
    )


def safe_model_id(model_id):
    return model_id if isinstance(model_id, str) and MODEL_PATTERN.fullmatch(model_id) else None


def safe_transport(transport):
    return transport if transport in (*LOCAL_TRANSPORTS, "ssh") else None


def failure_stage(code, stage=None, *, sent=False, preflight=False):
    # Auth errors can occur on any RPC, even after a prompt has been sent.
    if code in {"not_logged_in", "auth_persist"}:
        return "auth"
    if code.startswith(("ssh_", "sandbox_", "runner_")) or code in {
        "transport",
        "cli_missing",
        "cli_layout",
        "cli_path_absolute",
        "cli_not_executable",
        "cli_incompatible",
        "direct_workspace",
        "image_receipt_missing",
        "image_sha_mismatch",
    }:
        return "runner"
    if stage in {"runner", "handshake", "auth"}:
        return stage
    if not sent and code in _PREFLIGHT_CODES:
        return "preflight"
    if stage in _STAGES:
        return stage
    if code in {"queue_timeout", "cancelled", "closed"} and not sent:
        return "queue"
    if sent or code in {
        "model_unavailable",
        "model_ambiguous",
        "model_mismatch",
        "ask_unavailable",
    }:
        return "model"
    return (
        "preflight"
        if preflight and code not in {"startup_timeout", "protocol", "cli_failed", "process_exited"}
        else "unknown"
    )


def request_status(row):
    code = safe_code(row["code"])
    delivery = row["delivery"]
    if delivery not in {"sent", "not_sent", "unconfirmed"}:
        delivery = "sent" if row["wire_bytes"] else None
    if delivery is None:
        # Legacy records know certain preflight outcomes but cannot prove
        # whether a failed write reached the transport.
        delivery = (
            "not_sent"
            if (
                row["reset_reason"] == "preflight"
                or code in {"image_limit", "context_limit", "queue_timeout"}
            )
            else "unknown"
        )
    stage = (
        "success"
        if code == "ok" and delivery == "sent"
        else failure_stage(
            code,
            row["stage"],
            sent=delivery in {"sent", "unconfirmed"},
            preflight=row["reset_reason"] == "preflight",
        )
    )
    outcome = "成功" if stage == "success" else _STAGES[stage]
    reason = MESSAGES.get(code, "失败详情未记录。")
    if code == "timeout":
        reason = "模型请求超过等待时间。"
    if code == "ok":
        reason = "" if stage == "success" else "此记录缺少发送状态。"
    if delivery == "not_sent" and stage == "preflight" and code.startswith("image_"):
        outcome = "图片预检拒绝，Cursor 请求未发送"
        if code == "image_limit" and (label := _IMAGE_LIMITS.get(row["image_limit_kind"])):
            reason = f"本轮图片{label}超过限制。"
    return {
        "finished_at": row["finished_at"] or None,
        "day": row["day"],
        "model_id": safe_model_id(row["model_id"]),
        "transport": safe_transport(row["transport"]),
        "duration_ms": row["duration_ms"],
        "code": code,
        "stage": stage,
        "delivery": delivery,
        "outcome": outcome,
        "reason": reason,
    }
