# astrbot_plugin_cursor

使用官方 Cursor CLI，为 AstrBot 提供原生聊天 Provider。人格与历史仍由 AstrBot 管理。

> 0.2.0 待环境验收。24 项模型解析、15 项自动注册及 16 项历史复用回归已执行通过。真实 CLI、SSH、本地隔离、联通与计费尚未完成环境验收。

基础版本已合入默认分支 `main`。最新历史复用补丁位于 `fix/persistent-cli-provider`，见 [补丁说明](docs/HISTORY_PATCH.md)。

同一分支还包含自动注册补丁：保存明确的 `model_id` 后重载插件，即会新增来源和原生模型，不需要在面板手工添加。原有默认模型和备用模型保持不变。

## 本次修改

- 使用官方 `agent acp` 常驻进程，通过 JSON-RPC 维护会话，固定 Ask 和明确模型 ID。
- 对话历史连续时只发新增消息；system 独立比对，以差异增量更新现有会话。对话前缀变化、历史压缩或请求取消后重建会话。
- 使用等待队列和并发上限；排队超过 3 秒可回复排队提示，达到时限明确报错。
- 记录字节、耗时、排队和复用状态；不记录聊天正文、令牌或原始认证错误。
- 优先在 AstrBot 所在环境本地隔离运行；可选 SSH 专用运行器。

完整清单见 [CHANGELOG](CHANGELOG.md)，部署见 [安装指南](docs/INSTALL.md)。

## 配置

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `model_id` | 空 | 明确的 ACP modelId、唯一展示名或支持的管理端 ID；以 ACP 列表为准，新建原生 `id` 和 `model` 均使用解析后的 modelId；空/auto/default 不建模型 |
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

插件安装/重载时自动确保 `cursor_cli` 来源存在，Key、API Base 留空；模型只声明 `text`。重复加载会更新或复用已有配置，不重复新增。只在你自行选择后使用 Cursor，插件不切换当前默认模型，也不改 OpenAI OAuth、DeepSeek 或备用模型设置。

启动或重载时通过 ACP `session/new` 查询一次 `availableModels`，先精确匹配 `modelId`，再唯一匹配展示名或明确别名。`cursor-grok-4.6-high` 对应 `grok-4.6`，后者必须实际出现在 ACP 列表中；有歧义时报错。`cursor-admin models` 的管理端 ID 不作为最终判断依据，见 [模型 ID 解析规则](docs/MODEL_IDS.md)。

已存在同一 Cursor 模型、但原生 `id` 是自定义别名时，沿用该条目，`model` 使用解析后的 modelId，不重命名或另建一份，以免破坏默认/备用引用。ID 被其他提供商占用时提示冲突，不覆盖配置。细节见 [自动注册说明](docs/REGISTRATION.md)。

## 复用与并发的边界

- 有稳定 `conversation_id` 或 `session_id` 才复用会话；没有稳定标识时，不跨请求猜测会话归属。
- 前缀 hash 排除全部 `role=system`，完整比较其余对话；assistant 比对兼容首尾空白、完整 `<think>` / `<thinking>` 块被剥离和末尾闭合标签清理，不修改实际发送内容或返回的回复。
- system 变化通过一条增量说明发送；追加内容仅发送后缀，替换或删除会明确说明。system 消失（包括次轮传入 `None`）会撤回先前 system 条目。
- 重复上次请求，或已有回复的历史没有新 user 条目时，沿用该会话最近一次成功结果，不调用 CLI。没有可用缓存时安全返回空文本。仅有 system 变化时，差异保留到下次新 user 输入再发送。
- 同会话待执行请求都带 `request_sequence` 时可按序号重排；没有序号时按到达顺序。队列仅有 25 ms 的聚合窗口，不能重排已经开始的请求。
- Cursor ACP 的模型设置会影响共享运行时，因此此版本固定一个配置模型，不在并发请求间切换模型。
- 常驻不是永不重启：连接退出、取消未确认、达到会话数上限和模型实例清理时会轮换。轮换后的第一次请求会重新发送完整历史。

## 能力与费用

当前适配器声明文本能力。直接传入图片、音频或工具时抛出 `UnsupportedFeatureError`，有稳定的 `code` 和 `feature`，上层可以据此降级。此版本尚未将 AstrBot 工具注册到 Cursor，不能输出伪造的 `tool_calls`。

`input_bytes` 是完整规范化历史，`sent_input_bytes` 是本次发送的文本，`wire_bytes` 是 ACP JSON 帧大小，不含 SSH/TLS 开销。它们都不是 token 或账单金额。会话复用减少重复传输和重复入库，但不能保证历史 token 不再计费或额度下降一半。

缓存回复会记录 `reset_reason=cached_reply`，发送字节为 0。system 增量是提示词层面的更新，不能从 Cursor 既有上下文中物理删除旧指令。contexts 含 `tool_calls` 仍返回不支持错误；历史窗口裁剪仍会打断对话前缀。

真实模型可用性、缓存和套餐扣费应在 [Cursor 用量页](https://cursor.com/dashboard/usage) 独立核对。

## 检查命令

```bash
# 纯逻辑，无网络和子进程依赖
python tests/check_logic.py

# 历史复用回归，实际运行 Engine；ACP 对端为内存替身，无子进程或网络
python -m unittest -v tests.test_history_reuse

# 自动注册回归，使用配置保存/ProviderManager 测试替身
python -m unittest -v tests.test_registration

# 模型解析、自动注册和历史复用回归；使用模拟 ACP 列表，无网络或子进程
python -m unittest tests.test_models tests.test_registration tests.test_history_reuse

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
