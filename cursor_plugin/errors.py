"""Only fixed, public messages may cross the subprocess boundary."""

MESSAGES = {
    "config": "Cursor 配置无效；请检查插件配置和部署说明。",
    "model": "必须明确配置并使用同一个 Cursor 模型 ID；不支持 Auto 或自动切换。",
    "unsupported": "Cursor 插件第一版只支持纯文本；请关闭工具、图片和音频输入。",
    "context_limit": "文本上下文超过插件大小限制；请在 AstrBot 中压缩或重置会话。",
    "timeout": "Cursor CLI 调用超时；请求已终止。请先检查用量，避免立即重试产生重复费用。",
    "not_logged_in": "Cursor CLI 未登录或登录已失效；请管理员在专用容器中自行登录。",
    "cli_failed": "Cursor CLI 调用失败；原始输出已隐藏。请检查登录、指定模型权限、套餐额度及网络。",
    "protocol": "Cursor CLI 返回格式无效；请核对固定的 CLI 版本，不会自动换模型。",
    "output_limit": "Cursor CLI 输出超过安全大小限制；请求已终止。",
    "transport": "无法连接 Cursor 专用容器；请检查 SSH、固定主机公钥和客户端私钥。",
    "busy": "Cursor 正在处理其他请求；本次未调用模型，请稍后再试。",
    "closed": "Cursor Provider 已停止；请重新加载插件。",
}


class CursorError(RuntimeError):
    def __init__(self, code: str):
        self.code = code if code in MESSAGES else "cli_failed"
        super().__init__(MESSAGES[self.code])
