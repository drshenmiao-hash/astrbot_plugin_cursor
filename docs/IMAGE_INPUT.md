# Cursor ACP 图片输入验收

这一分支提供图片传输及验收脚本。正式注册的模型能力仍为 `text`。应先用隔离测试环境确认 `grok-4.6` 端到端读出图片信息，再启用该模型的图片能力。

## 已核实的能力

- ACP v1 的 `session/prompt` 可携带 `type: image`、`mimeType` 和 Base64 `data`。发送前须确认 `initialize` 返回 `agentCapabilities.promptCapabilities.image: true`。[ACP 内容块规范](https://agentclientprotocol.com/protocol/v1/content#image-content)
- 插件固定的官方 Cursor CLI `2026.11.13-eacade1` 本地启动后实际返回了 `image: true`。该版本的 Linux x86_64 发行包与 `runner/Dockerfile` 中的 SHA-256 一致。包内 `dist-package/178.index.js` 的 `processPrompt` 将收到的图片块解码、设置为 `UserMessage.selectedContext.selectedImages`，然后随该次 `agentClient.run` 发送。[官方 CLI 发行包](https://downloads.cursor.com/lab/2026.11.13-eacade1/linux/x64/agent-cli-package.tar.gz) · [Cursor ACP 文档](https://cursor.com/docs/cli/acp)
- Cursor 官方确认 Grok 4.6 会处理图片输入，并指出极端尺寸会被模型拒绝。这项公告与 CLI 的协议能力属于不同层面的证据。工作区的 CLI 未登录，`session/new` 返回 `-32000`，因此尚未获得这个模型经该通道的真实识图结果。[Cursor 官方说明](https://forum.cursor.com/t/image-error-the-model-provider-could-not-read-an-image-in-this-conversation-animated-gifs-corrupted-files-and-images-with-extreme-dimensions-are-rejected-start-a-new-chat-without-that-image-to-continue/172098/5)

## 传输和限制

AstrBot 准备的本地图片由插件进程读取。允许 AstrBot 临时媒体目录内的普通文件，以及内嵌 Base64 图片。插件拒绝目录外路径、符号链接、硬链接及未经 AstrBot 准备的网络 URL。图片以 ACP 内容块内嵌到现有连接，本地路径和文件名无需传给远端。插件只在内存中处理图片，AstrBot 原有的媒体文件清理继续生效。验收脚本退出时删除自己生成的测试文件。

接收静态 PNG、JPEG、WebP。每张最多 2 MiB，每份请求最多 4 张、原始图片共计最多 4 MiB，包括请求带来的历史图片。每张最多 400 万像素、最短边至少 8 像素。格式、解码、来源和上限错误会在模型调用前报告。ACP 缺少图片能力、上游拒绝或请求超时也会明确失败。会话复用时只附上增量需要的图片。

## 测试机验收

在能运行 AstrBot 的隔离测试环境使用此修复分支。先自行登录测试专用 Cursor 运行端。按 `tests/environment_config.example.json` 准备独立 JSON，明确将 `model_id` 设为 `grok-4.6`，填写测试环境现有连接或专用登录路径。脚本读取这份 JSON，并使用临时 AstrBot 数据目录。

```bash
ASTRBOT_ROOT="$(mktemp -d)" python -m unittest tests.test_image_input -v
python -m tests.check_cursor_images --config /绝对路径/cursor-image-test.json --allow-model-requests
```

第二条会调用真实 Cursor 模型，可能消耗用量。它临时在同一个测试进程内允许 `grok-4.6` 接收图片，并验证仅图片、图文混合、多图及顺序、损坏与超限拒绝、纯文本回归。用例包含 PNG、JPEG 和 WebP。真实识图用的随机验证码只在像素里出现，检查输出只显示匹配结果或错误码。超时和会话回归由第一条的模拟 ACP 测试覆盖。只有通过真实识图测试，才可把相应模型加入正式的已验证模型列表。
