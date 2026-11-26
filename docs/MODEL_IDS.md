# ACP 模型 ID 解析

`cursor-admin models` 属于管理端名单，不能证明同一 ID 可以发给 ACP。插件启动、重载或显式校验有效的 `model_id` 时，通过已登录 CLI 的 ACP `session/new` 查询一次 `models.availableModels`，以每个条目的 `modelId` 为准。

查询不发送 `session/prompt`。已有连接及账号设置相同的运行时会复用；没有可用运行时时使用临时查询进程并关闭。正常聊天仍使用常驻进程。每个新聊天会话都会在新列表中精确核对已解析的 modelId。该 ID 消失时，聊天请求返回模型不可用。

## 确定的匹配规则

1. 配置值先与 `modelId` 精确匹配，优先于其它条目的展示名。
2. 精确匹配失败后，收集以下规则命中的 modelId；最终必须唯一，且实际存在于 ACP 列表：
   - `name` 或 `displayName`，允许大小写及首尾空白差异。
   - 完整的 `cursor-<modelId>` 写法。
   - 明确别名 `cursor-grok-4.6-high → grok-4.6`。
3. 多个不同候选报 `model_ambiguous`；没有候选报 `model_unavailable`。错误及 AstrBot 日志列出可用 modelId，歧义还列出候选。

例如，ACP 返回 `{"modelId":"grok-4.6","name":"Grok 4.6"}` 时，`grok-4.6`、`Grok 4.6` 和 `cursor-grok-4.6-high` 都解析为 `grok-4.6`。若 ACP 还返回 `grok-4.6-high`，管理端写法会同时命中两个不同 ID，因此报歧义而不猜测。

不做包含匹配、模糊匹配，也不任意移除 `-high`、`-ultra` 或版本后缀。Grok 的已知映射只解析模型身份，不据此开启其它推理或计费参数。未知别名请改填错误中列出的确切 modelId。

`auto`、`auto-smart`、空值和 `default` 不允许选择；即使 ACP 列表中存在 auto，或其展示名是 Automatic，也不能通过别名绕过。完整错误名单仍反映 ACP 实际列表，面板的可选列表排除这些自动路由 ID。

## 注册和配置保护

插件设置中的 `model_id` 用于首次校验和注册。新建原生模型的 `id` 和 `model` 均使用解析后的 modelId，例如 `grok-4.6`。已有别名且 model 已正确时沿用原生 ID。旧 model 是可唯一解析的管理端 ID 时，只纠正 model 字段。

在 AstrBot 中把其他可用模型添加到 **Cursor CLI** 来源后，即可在系统配置或会话中选择。聊天使用当前 Provider 的模型，单次显式提供的模型参数优先。每个新 ACP 会话使用运行端返回的目录验证模型并确认选择。切换模型时按当前聊天历史建立正确模型的会话。若 CLI 目录没有 `grok-4.7`，插件会返回 `model_unavailable`；实际可用性取决于当前账号和 CLI。

模型不可用、有歧义、未登录或连接失败时，不注册另一个模型代替。默认模型、备用模型以及 OpenAI OAuth、DeepSeek 配置不改。

## 回归

```bash
python -m unittest tests.test_models tests.test_registration tests.test_history_reuse
```

24 项模型解析、15 项注册和 16 项历史复用回归，共 55 项。完整实际输出见 [MODEL_ID_TEST_OUTPUT.txt](MODEL_ID_TEST_OUTPUT.txt)。

测试以模拟的 ACP `session/new` 回复运行真实模型解析、ACP 选择参数构造、配置注册及重载逻辑，并断言真正构造的选择值为 `grok-4.6`、查询不发送提示词、失败不选择其他模型。ProviderManager、配置保存及传输使用测试替身，不调用真实 CLI、SSH、网络或账户，不代表真实套餐计费验收。
