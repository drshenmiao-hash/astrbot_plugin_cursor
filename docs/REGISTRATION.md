# 自动注册来源与原生模型

自动注册已包含在默认分支 `main`，安装或更新后保存插件配置并重载即可使用。

安装或重载时：

1. 在 `provider_sources` 中查找 `type=cursor_cli` 的来源，优先使用 ID 为 `cursor_cli` 的条目，否则复用已有 Cursor 来源。没有则创建。
2. 来源 Key、API Base 留空。模型只声明 `modalities=["text"]`。
3. 插件配置 `model_id` 明确有效时，先从 ACP 查询一次模型列表并按 [解析规则](MODEL_IDS.md) 得到 modelId，再查找同 ID 或同 Cursor 模型的条目。有则更新/复用，没有才新增。新建条目的 `id` 和 `model` 均使用解析后的 ACP modelId，不写入管理端 ID 或展示名，不自动替换成别的模型。
4. 使用 AstrBot 配置的 `save_config_async()` 保存变更，同步 ProviderManager 的来源、模型列表。没有变更不重复保存。保存快照被更新版本取代时重试一次；保存失败则回滚自己这次仍未被并发修改的字段，给出固定错误。
5. 冷启动时由 AstrBot 随后加载模型；运行中的插件安装或重载会加载自己的目标模型或刷新已有 Cursor 实例，最后恢复之前的当前模型选择，包括原先未选择模型的状态。

`model_id` 为空、全空白、auto、auto-smart 或 default 时，仅确保来源存在，并在 AstrBot 日志明确提示不创建模型。这些无效输入不会触发模型发现，也不会自动选套餐模型。有效输入需要先通过 ACP 校验；没有登录或 CLI 不可达时明确失败。已有模型不删除，当前默认和备用引用不改。

## 去重与保护范围

- 现有 Cursor 来源不另建，保留来源 ID 和用户禁用状态。
- 已有相同 `model`、原生 ID 却是自定义别名时沿用该条目，不改名或重复创建。`model` 使用解析后的 ACP modelId；旧 `model` 为可唯一解析的管理端 ID 时更新该字段，也不重命名原生 ID。这样默认、会话和备用列表中的已有引用不失效。
- 同 ID 被其他提供商占用时，不覆盖或自动改名。来源 ID 冲突会报错；模型 ID 冲突会明确提示并跳过模型创建。
- 不修改 OpenAI OAuth / DeepSeek 的来源、密钥、模型能力或实例；不修改 agent、provider_settings、会话偏好或备用列表。已禁用的 Cursor 条目不会被自动启用。
- 修改 `model_id` 不会把旧模型引用改到新模型上。新增的新模型需要由用户自行选择；这不是自动切换默认模型。

## 参考实现与边界

参考 `astrbot_plugin_openai_oauth` 提交 `173f343b8ffff76ddee288ab71dfcf7c57a5c589`：

- `src/runtime.py` 的 `_build_default_source_config()`、`initialize()` 及 ProviderManager 列表同步。
- `src/provider_registry.py` 的 `_commit_configuration()` 快照、异步保存及条件回滚。

该参考版本自动确保来源，并迁移/保留已有模型；Cursor 按本插件的明确 `model_id` 补上原生模型新增。没有复制 OAuth 账号管理、凭据加密或配置服务拦截器；没有修改 AstrBot 核心或其他插件。

接口依据 AstrBot 4.29.4。运行中但没有任何模型的管理器，使用该版本已存在的 `_mcp_init_task` 判断 ProviderManager 是否已完成初始化；不安装额外生命周期钩子。

## 回归结果

```bash
python -m unittest -v tests.test_registration tests.test_history_reuse
```

实际运行 31 项通过：15 项注册回归、16 项历史复用回归。完整输出见 [REGISTRATION_TEST_OUTPUT.txt](REGISTRATION_TEST_OUTPUT.txt)。

注册测试运行真实 `runtime.configure/initialize` 和配置规划/保存/回滚代码；AstrBot 配置保存、ProviderManager 及生成的 Provider 实例使用明确的内存替身。覆盖首次创建、重复加载、空/auto、默认/备用及 OAuth/DeepSeek 不变、text 能力、已有配置更新、别名、禁用、ID 冲突、冷/热启动和保存失败。没有调用 CLI、SSH、网络或真实账户；未覆盖实际 AstrBot WebUI 或生产安装。

回退时恢复目标旧版本的完整插件目录并重载。已写入的配置不会被自动删除，以免破坏用户后来设置的引用；如需移除注册项，先确认没有默认/备用/会话引用，再在 AstrBot 模型设置中处理。
