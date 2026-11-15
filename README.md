# astrbot_plugin_cursor

使用官方 Cursor CLI，为 AstrBot 提供原生聊天 Provider。人格与历史仍由 AstrBot 管理。

> 0.2.0 开发分支。纯逻辑检查已执行；常驻进程、SSH、本地隔离、真实联通与计费尚未完成环境验收。

本次代码在 [`fix/persistent-cli-provider`](https://github.com/drshenmiao-hash/astrbot_plugin_cursor/tree/fix/persistent-cli-provider)。默认分支仍是旧版；测试本次修改请下载该分支或使用本次提交的源码包。

## 本次修改

- 使用官方 `agent acp` 常驻进程，通过 JSON-RPC 维护会话，固定 Ask 和明确模型 ID。
- 历史连续时只发新增消息。完整前缀 hash 变化、历史压缩、人格变化或请求取消后重建会话。
- 使用等待队列和并发上限；排队超过 3 秒可回复排队提示，达到时限明确报错。
- 记录字节、耗时、排队和复用状态；不记录聊天正文、令牌或原始认证错误。
- 优先在 AstrBot 所在环境本地隔离运行；可选 SSH 专用运行器。

完整清单见 [CHANGELOG](CHANGELOG.md)，部署见 [安装指南](docs/INSTALL.md)。

## 配置

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `model_id` | 空 | 必须填写明确、套餐范围已确认的模型 ID；原生模型使用同一个 ID |
| `transport` | `auto` | 有本地 CLI 和 bubblewrap 时选 local，否则选 ssh；隔离失败不会裸跑 |
| `cli_binary` | 自动查找 | 本地官方 `agent` / `cursor-agent` 绝对路径 |
| `cursor_auth_path` | `~/.config/cursor/auth.json` | 本地专用浏览器登录文件 |
| `bubblewrap_binary` | `/usr/bin/bwrap` | 本地文件隔离程序 |
| `max_concurrency` | `2` | 同时执行上限 1–8，同一会话始终串行 |
| `queue_capacity` | `64` | 等待队列容量；满时等待空位，而非立即丢弃 |
| `queue_timeout_seconds` | `120` | 入队、同会话等待和启动等待的总预算 |
| `timeout_seconds` | `120` | 开始执行后的超时；发送 ACP 取消，5 秒未确认则重置运行时 |
| `context_max_bytes` | `262144` | 完整规范化上下文的 UTF-8 字节上限；超限报错，不截断 |
| `output_max_bytes` | `2097152` | 回复字节上限 |
| `max_sessions` | `128` | 达到会话数量上限后，待运行中的请求结束再轮换进程 |
| `ssh_host` / `ssh_port` | `cursor-cli` / `2222` | 仅用于 ssh 路径 |
| `identity_file` | `/run/cursor-client/id_ed25519` | 专用客户端私钥 |
| `known_hosts_file` | `/run/cursor-client/known_hosts` | 固定主机公钥 |
| `ssh_binary` | `/usr/bin/ssh` | OpenSSH 客户端 |

在 AstrBot 添加 `cursor_cli` 来源，Key、API Base 留空，再添加与 `model_id` 相同的原生聊天模型。已有 OpenAI OAuth 配置和备用模型设置保持原样。

## 复用与并发的边界

- 有稳定 `conversation_id` 或 `session_id` 才复用会话；没有稳定标识时，不跨请求猜测会话归属。
- 比较整个历史前缀的 hash。长度和末几条只用于诊断，不能代替完整比较。
- 同会话待执行请求都带 `request_sequence` 时可按序号重排；没有序号时按到达顺序。队列仅有 25 ms 的聚合窗口，不能重排已经开始的请求。
- Cursor ACP 的模型设置会影响共享运行时，因此此版本固定一个配置模型，不在并发请求间切换模型。
- 常驻不是永不重启：连接退出、取消未确认、达到会话数上限和模型实例清理时会轮换。轮换后的第一次请求会重新发送完整历史。

## 能力与费用

当前适配器声明文本能力。直接传入图片、音频或工具时抛出 `UnsupportedFeatureError`，有稳定的 `code` 和 `feature`，上层可以据此降级。此版本尚未将 AstrBot 工具注册到 Cursor，不能输出伪造的 `tool_calls`。

`input_bytes` 是完整规范化历史，`sent_input_bytes` 是本次发送的文本，`wire_bytes` 是 ACP JSON 帧大小，不含 SSH/TLS 开销。它们都不是 token 或账单金额。会话复用减少重复传输和重复入库，但不能保证历史 token 不再计费或额度下降一半。

真实模型可用性、缓存和套餐扣费应在 [Cursor 用量页](https://cursor.com/dashboard/usage) 独立核对。

## 检查命令

```bash
# 纯逻辑，无网络和子进程依赖
python tests/check_logic.py

# 一份环境脚本，使用模拟 ACP 进程，不调用 Cursor
python tests/check_environment.py --mock

# 真实环境，会发起模型请求并可能计费
python tests/check_environment.py --config /path/to/plugin-config.json --allow-model-requests

# 仅诊断依赖，不发送模型提示词
python -m cursor_plugin.diagnostics --config /path/to/plugin-config.json
```

机器人管理员可运行 `/cursor_doctor`。每项环境检查打印 PASS、FAIL 或 SKIP，并带 RAW 结果或异常堆栈；不会把认证报文或密钥打印出来。详细说明见 [测试指南](docs/ACCEPTANCE.md)。

## 环境与来源

接口目标为 AstrBot 4.29.4；Python 3.10+。本地模式依赖 Linux bubblewrap 与可用的命名空间隔离；容器配方为 Linux amd64。无需安装 Cursor 桌面版，无新增 Python 依赖。

Provider 注册和重载改编自 [astrbot_plugin_openai_oauth](https://github.com/wcqqq1214/astrbot_plugin_openai_oauth)，来源见 [NOTICE](NOTICE)。使用官方 [Cursor ACP](https://cursor.com/docs/cli/acp) 协议；不修改 AstrBot 核心，不搭建 OpenAI 兼容 HTTP 桥接。

[AGPL-3.0](LICENSE)。`/cursor_source` 提供本版本源码地址。
