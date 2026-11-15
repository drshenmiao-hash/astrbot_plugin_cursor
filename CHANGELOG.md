# 变更记录

## system 时钟比对（当前分支补丁）

- system_delta 仅在比对时忽略明确的日期/时刻/时区元数据行。纯时钟变化不产生 system replace/append，不重建会话。
- 增量提示词通过独立当前时间字段保留本轮时钟，首次全文和真实 replace 内容仍使用原文；人设、规则、记忆变化仍推送。
- 保守支持多种中英文标签、日期、ISO 时间和时区格式，非法/歧义/混合业务正文不剥离，不误删普通数字。
- 更新原时间戳测试，新增 10 项时钟回归，和统计/模型/注册/历史合跑 79 项通过，完整输出见 `docs/SYSTEM_CLOCK_TEST_OUTPUT.txt`。

## 插件本地统计（当前分支补丁）

- request_finished 元数据异步落入插件专属 SQLite，成功/失败均记录；按总计、匿名会话、UTC 日期、实际 ACP modelId 汇总。
- 新增仅管理员可用的 `/cursor_usage [total|session|day|model] [具体值]`。明确标注本地统计，不查询或声称官方账单/套餐消耗。
- 缓存/排队等未记录到 ACP 发送的请求单独分组；不增加模型请求或并发，不改缓存 key、默认/备用及其它提供商配置。
- 14 项统计回归与已有 55 项合跑共 69 项通过，完整输出见 `docs/LOCAL_USAGE_TEST_OUTPUT.txt`。

## ACP 模型 ID 解析（当前分支补丁）

- 启动/重载/显式校验时通过 ACP `session/new` 拉取一次 availableModels；最终以 modelId 为准，不用管理端 models 名单代替。
- 先精确匹配 modelId，再唯一匹配展示名、`cursor-<modelId>` 或明确别名 `cursor-grok-4.6-high → grok-4.6`；不任意剥离后缀，有歧义则报 model_ambiguous。
- 不存在时报 model_unavailable，错误和日志均包含可用 modelId；禁止 auto、空、default，包括通过展示名间接选择 auto。
- ACP 选择参数和新建原生模型的 id/model 都使用解析后的 modelId；已有别名复用不改名，旧管理端 model 字段可原位纠正；默认/备用及其他提供商配置不变。
- 新增 24 项模型解析回归，和 15 项注册、16 项历史复用合跑共 55 项通过，使用模拟 ACP 列表，不连接真实 Cursor。

## 自动注册来源与模型（开发分支补丁）

- 沿用 OpenAI OAuth 插件的来源工厂、配置快照/保存/回滚、管理器同步和重载保留选择流程；仅写入 Cursor 自己的来源和模型。
- 安装/重载自动确保 `cursor_cli` 来源存在，Key/API Base 留空；明确配置模型时自动新增原生模型，新建 `id` 与 `model` 均为配置的 `model_id`，能力只声明 text。
- 空/auto 不创建模型，给出明确提示；不自动选择模型，不改默认/备用、OpenAI OAuth 或 DeepSeek 配置。
- 同来源/同模型去重，保留已有别名和禁用状态；其他提供商 ID 冲突时不覆盖。冷启动交给 AstrBot 后续加载，热重载即时装载自己的目标模型。
- 15 项注册回归及 16 项历史复用回归全部通过，输出见 `docs/REGISTRATION_TEST_OUTPUT.txt`。测试使用配置和 ProviderManager 替身，不代表真实 WebUI 或 CLI 验收。

## 历史复用修复（开发分支补丁）

- 根据用户提供的 Lumi 现网观察，system 不再参与对话前缀匹配；system 变更作为一条增量发送，不因此新建会话。
- assistant 历史匹配兼容 `strip()`、完整 think/thinking 块剥离及末尾闭合标签清理；不修改 user 文本、原始回复或实际传输内容。
- 缓存最近一次成功请求的对话 hash 和结果，兼容原请求重放、已含 assistant 回复的相同历史；无新 user 时不再报 `empty_input` 或调用 CLI。
- 仅有 system 变化时延迟发送，直到出现新 user；只在模型成功后提交新的 system 状态。
- 不改 CLI/SSH/隔离/调度基建。初版只做静态检查；后续已补齐并执行 16 项内存回归，覆盖 system 变化、记忆位置、重放、延迟更新和 think 归一化，完整输出见 `docs/HISTORY_REGRESSION_OUTPUT.txt`。

## 0.2.0（开发中）

1. 后端切换为官方 `agent acp` 常驻进程，连续会话只发送新增消息；对话历史前缀不匹配时重建会话。
2. 用有界等待队列替换忙时拒绝，提供并发上限、排队提示和独立排队超时。同一会话串行，已排队且带序号的请求按序号排序。
3. 图片、音频和 AstrBot 工具调用使用带 `code`、`feature` 的能力错误；不把 Cursor 内部工具伪装为 AstrBot `tool_calls`。
4. 记录完整上下文字节、实际增量文本字节、ACP 帧字节、输出字节、排队及总耗时、会话与进程复用状态，不记录正文或凭据。
5. 增加本地隔离模式；具备 CLI 和 bubblewrap 时不需要 SSH。保留 SSH 路径，并分别诊断密钥、主机校验、SSH 版本、登录卷和构建哈希。
6. `context_max_bytes` 可配置，默认 256 KiB；超限报错，不截断。
7. 增加纯逻辑检查与一个环境检查脚本，环境检查逐项输出 PASS/FAIL/SKIP 和 RAW 结果。

运行时、Cursor 真实联通与套餐计费仍待环境验收。本版本尚未发布为稳定版本。
