"""Typed, fixed errors for routing and diagnostics; upstream payloads stay private."""

import logging

MESSAGES = {
    "registration_save_failed": "Cursor 来源或模型配置未能保存，请检查 AstrBot 配置写入权限并重载插件。",
    "usage_unavailable": "Cursor 本地统计不可用，请检查插件数据目录权限。",
    "source_id_conflict": "cursor_cli 来源 ID 已被其他提供商占用；未覆盖已有配置，请处理冲突后重载。",
    "config": "Cursor 配置无效，请检查插件配置。",
    "model": "请明确配置同一个 Cursor 模型 ID；不支持 Auto 或默认路由。",
    "model_unavailable": "指定模型不在 Cursor 返回的模型列表中；没有自动更换模型。",
    "model_ambiguous": "模型名称或管理端 ID 对应多个 ACP modelId，请明确填写 modelId；未选择模型。",
    "model_not_validated": "Cursor model_id 尚未通过 ACP 校验，请先完成登录并重载插件。",
    "model_mismatch": "Cursor 未确认指定模型，已停止请求。",
    "connection_stalled": "Cursor 生成连接停滞；请稍后重试或使用备用模型。",
    "ping_timeout": "Cursor 生成连接心跳超时；请稍后重试或使用备用模型。",
    "upstream_retryable": "Cursor 生成流暂时失败；请稍后重试或使用备用模型。",
    "ask_unavailable": "Cursor 未保持 Ask 模式，已停止请求。",
    "unsupported": "Cursor 当前适配器不支持此输入能力。",
    "context_limit": "完整上下文超过 context_max_bytes，已报错；不会截断或悄悄丢弃历史。",
    "timeout": "Cursor 请求超时，已发出取消；请检查诊断记录和用量后再决定是否重试。",
    "queue_timeout": "请求排队超时，本次没有开始模型调用；请稍后重试。",
    "startup_timeout": "Cursor 进程或登录握手超时，尚未开始模型调用。",
    "not_logged_in": "Cursor 未登录或登录已失效，请管理员自行完成登录。",
    "quota_exhausted": "Cursor 额度不足；本次不自动重试，请检查套餐用量。",
    "rate_limited": "Cursor 请求受到限流；本次不自动重试。",
    "cli_failed": "Cursor 调用失败；原始输出已隐藏，请运行 /cursor_doctor 检查。",
    "protocol": "Cursor ACP 返回格式不兼容，请检查 CLI 版本。",
    "output_limit": "Cursor 输出超过安全限制，已发出取消请求。",
    "unsupported_output": "Cursor 返回了不支持的输出类型。",
    "empty_input": "没有可发送的文本上下文。",
    "empty_output": "Cursor 没有返回文本回复。",
    "transport": "Cursor 连接无法启动，请运行 /cursor_doctor。",
    "process_exited": "Cursor 常驻进程退出；本次不会自动重试。",
    "runner_in_use": "Cursor 运行器已被其他实例占用，请使用独立登录卷或停止重复实例。",
    "cancelled": "Cursor 请求已取消。",
    "cancel_unconfirmed": "远端取消未确认，已关闭连接；请核对用量。",
    "closed": "Cursor Provider 已停止。",
    "cleanup_timeout": "Cursor 进程清理未在期限内完成。",
    "cleanup_unverified": "未收到 Cursor 运行器清理回执，无法确认远端进程和锁已释放。",
    "runner_cleanup_unsupported": "当前 Cursor 运行器未提供清理回执，请使用与插件配套的运行器版本。",
    "auth_persist": "Cursor 登录刷新结果未能保存。",
    "ssh_missing": "缺少 OpenSSH 客户端。",
    "ssh_key_missing": "缺少专用 SSH 客户端私钥。",
    "ssh_key_permissions": "SSH 私钥权限过宽，应仅允许所有者访问。",
    "ssh_host_key_missing": "缺少固定的 SSH 主机公钥文件。",
    "ssh_host_key_mismatch": "SSH 主机公钥校验失败；未自动信任新主机。",
    "ssh_auth_denied": "SSH 客户端密钥认证失败。",
    "ssh_unreachable": "SSH 网络连接失败。",
    "cli_missing": "未找到官方 Cursor CLI。",
    "cli_layout": "Cursor CLI 安装目录不符合官方发行包结构。",
    "sandbox_missing": "本地模式缺少 bubblewrap；未降级为无隔离执行。",
    "sandbox_unavailable": "本地文件隔离不可用，请检查内核或容器权限，或使用 SSH 运行器。",
    "sandbox_path_conflict": "AstrBot 数据位于本地沙盒允许运行库路径中，请使用专用运行器。",
    "image_sha_mismatch": "Cursor 运行器文件与构建校验记录不符。",
    "image_receipt_missing": "Cursor 运行器缺少构建版本或哈希校验记录。",
}


class CursorError(RuntimeError):
    retryable = False

    def __init__(self, code):
        self.code = code if isinstance(code, str) and code in MESSAGES else "cli_failed"
        super().__init__(f"[cursor:{self.code}] {MESSAGES[self.code]}")


class UnsupportedFeatureError(CursorError):
    """Stable capability failure; upstream can select a supported provider."""

    def __init__(self, feature):
        self.feature = (
            feature if feature in {"image", "audio", "tool_calls", "content"} else "content"
        )
        self.code = "unsupported_" + self.feature
        RuntimeError.__init__(
            self,
            f"[cursor:{self.code}] 当前 Cursor ACP 适配器不支持 {self.feature}，请使用支持该能力的模型。",
        )


class CursorStreamError(CursorError):
    retryable = True


async def retry_stream_errors(operation, *, request_max_retries=None):
    """At most one retry of a detected stream error; other failures propagate."""
    retries = 0 if isinstance(request_max_retries, int) and request_max_retries <= 1 else 1
    for attempt in range(retries + 1):
        try:
            return await operation()
        except CursorStreamError as exc:
            logging.getLogger("astrbot").warning(
                "cursor_stream_failure code=%s attempt=%d max_attempts=%d retryable=true",
                exc.code,
                attempt + 1,
                retries + 1,
            )
            if attempt == retries:
                raise
