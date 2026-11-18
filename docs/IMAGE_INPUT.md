# Cursor ACP 图片输入

插件把图片附在当前请求里，发给 `model_id` 配置且经 ACP 解析的模型。不存在模型白名单或额外的图片能力探测请求。模型或运行端明确拒绝图片能力时返回 `image_unsupported`，聊天提示可在插件设置的 **图片不支持时的回复** 中自定义。坏图、超限、超时和其他上游错误保留各自的错误码。备用模型由 AstrBot 按已有配置处理。

## 已核实的能力

- ACP v1 的 `session/prompt` 可携带 `type: image`、`mimeType` 和 Base64 `data`。发送前须确认 `initialize` 返回 `agentCapabilities.promptCapabilities.image: true`。[ACP 内容块规范](https://agentclientprotocol.com/protocol/v1/content#image-content)
- 插件固定的官方 Cursor CLI `2026.11.13-eacade1` 本地启动后实际返回了 `image: true`。该版本的 Linux x86_64 发行包与 `runner/Dockerfile` 中的 SHA-256 一致。包内 `dist-package/178.index.js` 的 `processPrompt` 将收到的图片块解码、设置为 `UserMessage.selectedContext.selectedImages`，然后随该次 `agentClient.run` 发送。[官方 CLI 发行包](https://downloads.cursor.com/lab/2026.11.13-eacade1/linux/x64/agent-cli-package.tar.gz) · [Cursor ACP 文档](https://cursor.com/docs/cli/acp)
- Cursor 官方确认 Grok 4.6 会处理图片输入，并指出极端尺寸会被模型拒绝。Grok 4.6 的真实识图由用户在独立测试机上确认。其他模型由其当前 Cursor 运行端决定。若上游正常回复却静默忽略图片，ACP 当前没有确认模型实际读取图片的字段。[Cursor 官方说明](https://forum.cursor.com/t/image-error-the-model-provider-could-not-read-an-image-in-this-conversation-animated-gifs-corrupted-files-and-images-with-extreme-dimensions-are-rejected-start-a-new-chat-without-that-image-to-continue/172098/5)

## 传输和限制

AstrBot 准备的本地图片由插件进程读取。允许 AstrBot 临时媒体目录内的普通文件，以及内嵌 Base64 图片。插件拒绝目录外路径、符号链接、硬链接及未经 AstrBot 准备的网络 URL。图片以 ACP 内容块内嵌到现有连接，本地路径和文件名无需传给远端。插件只在内存中处理图片，AstrBot 原有的媒体文件清理继续生效。验收脚本退出时删除自己生成的测试文件。

接收静态 PNG、JPEG、WebP。当前轮最多 4 张、每张最多 2 MiB、共计最多 4 MiB。每张最多 400 万像素，最短边至少 8 像素。历史图片仅保留用于比对的标识。会话重建时保留历史文本，并用 `historical_images_not_attached` 标明哪些消息带过图片，重建后的模型只获得这些旧图对应的文字上下文。当前轮引用或重发的图片照常传输。

`image_limit` 日志的 `image_limit_kind` 区分 `count`、`file_bytes`、`total_bytes`、`pixels`、`frame_bytes`。上限和值由 `image_limit_max` 和 `image_limit_actual` 报告。`sent_image_blocks` 和 `sent_image_base64_bytes` 记录实际发送的图片。`sent_input_bytes` 仍指文本，`wire_bytes` 包括文本、图片 Base64 和 JSON 封装。日志不包含图片、路径或内容哈希。

`history_comparison` 可用 `request_id` 关联 `request_finished`。`history_change` 区分窗口缩短、窗口移位和单条变化。单条变化会报告首个差异的位置及变化字段 `role`、`content` 或 `images`。需要重建的真实窗口变化与历史编辑仍会重建会话。

## 可选的测试机验收

在能运行 AstrBot 的隔离测试环境，先自行登录专用 Cursor 运行端。按 `tests/environment_config.example.json` 准备独立 JSON，指定要测试的 `model_id` 和对应的连接或专用登录路径。脚本读取这份 JSON，并使用临时 AstrBot 数据目录。

```bash
ASTRBOT_ROOT="$(mktemp -d)" python -m unittest tests.test_image_input -v
python -m tests.check_cursor_images --config /绝对路径/cursor-image-test.json --allow-model-requests
```

第二条会调用指定的真实 Cursor 模型，可能消耗用量。验证仅图片、图文混合、多图及顺序、损坏与超限拒绝、纯文本回归。用例包含 PNG、JPEG 和 WebP。随机验证码只在图片像素里出现，检查输出只显示匹配结果或错误码。超时和会话回归由第一条的模拟 ACP 测试覆盖。
