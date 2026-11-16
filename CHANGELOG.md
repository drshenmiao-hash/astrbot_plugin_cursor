# 变更记录

## 0.2.0

### 原生 Provider 与模型

- 使用官方 `agent acp` 常驻进程，固定 Ask 模式和明确模型，不传 `--force`。
- 安装/重载自动确保 `cursor_cli` 来源和原生模型存在，Key/API Base 留空，仅声明 text；已有来源、模型、别名与禁用状态会保留或复用。
- 启动/重载从 ACP 的 availableModels 校验 modelId，支持唯一展示名及明确管理端别名；`cursor-grok-4.6-high` 映射到列表中的 `grok-4.6`，歧义或不存在时给出可用 ID，不自动换模型。
- 新建原生模型的 id/model 使用解析后的 ACP modelId，不改变当前默认、备用、OpenAI OAuth 或 DeepSeek 配置。
- 注册、配置保存/回滚、同步和重载流程参考 OpenAI OAuth 插件，来源见 [NOTICE](NOTICE)。

### 会话与队列

- 连续对话只发送新增消息，前缀 hash 排除 system；assistant 比对兼容首尾空白、think/thinking 块及末尾闭合标签清理，不改原始正文。
- system 实质变化作为增量发送；纯时钟变化不触发 replace/append，每轮仍以最小当前时间字段传入最新时钟。不明确的格式保留原文。
- 重放已成功处理的请求或没有新 user 输入时沿用缓存回复，不再报 empty_input 或额外调用模型。
- 有界等待队列、并发上限、排队提示和独立排队超时，同一会话串行。上下文上限默认 256 KiB，可配置，超限报错而不截断。

### 本地统计与部署

- request_finished 元数据异步写入插件专属 SQLite，成功/失败均记录；管理员可按总计、匿名会话、UTC 日、实际 ACP modelId 查询。
- 新增 `/cursor_usage [total|session|day|model] [具体值]`。统计不是 Cursor 账单或套餐余额，不查询官方消耗。
- 支持本地 CLI + bubblewrap 隔离，SSH 专用运行器可选；提供 `/cursor_doctor`、容器健康检查和环境诊断脚本。
- 纯文本能力错误可供上层降级；尚未接入 AstrBot 工具调用。不记录聊天正文或凭据。

### 验证

- 时钟 10 项、本地统计 14 项、模型解析 24 项、注册 15 项、历史复用 16 项，共 79 项内存回归通过，见 [完整输出](docs/SYSTEM_CLOCK_TEST_OUTPUT.txt)。
- 另有 23 项纯逻辑检查通过，见 [原始输出](docs/LOGIC_OUTPUT.txt)。
- 自动化使用内存 ACP/ProviderManager 替身和临时数据，不代表真实环境各路径或套餐计费结论；本地字节和次数也不等于官方账单。
