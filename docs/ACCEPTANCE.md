# 由使用者执行的验证与验收

**当前状态：所有下列项目均未执行、未验收。** 本次仅做源码阅读、静态语法和格式检查；没有启动 AstrBot、Provider 测试、容器或官方 CLI，也没有登录、发送真实请求或产生可确认的 Cursor 用量。

## A. 离线模拟 CLI

建议在独立的测试 AstrBot 容器内，使用其自身 Python 和依赖运行。这里的 `mock_cli.py` 和 `mock_ssh.py` 均为本仓库提供的假程序，绝不连接 Cursor。测试的凭据也是无效占位字符串，不能用于登录。

若测试实例把插件放在 `/AstrBot/data/plugins/astrbot_plugin_cursor`，官方 AstrBot 4.29.4 镜像的命令示例：

```bash
# 替换 astrbot-test 为你的独立测试容器名。不要针对生产容器执行。
docker exec -w /AstrBot/data/plugins/astrbot_plugin_cursor astrbot-test \
  python -m unittest discover -s tests -v
```

源码部署可在安装 AstrBot 依赖的环境中，从本插件根目录执行：

```bash
python -m unittest discover -s tests -v
```

如果提示找不到 `astrbot`，应换用安装了**真实 AstrBot** 的解释器。`test_provider.py` 和 `test_lifecycle.py` 导入实际 Provider/LLMResponse/注册表，不用伪造 AstrBot 类绕过接口校验。仅运行不依赖 AstrBot 的边界测试，可使用：

```bash
python -m unittest tests.test_runner -v
```

测试内容：

| 项目 | 脚本和预期 |
| --- | --- |
| Provider 调用 | 实际 `CursorCLIProvider.text_chat()` → 模拟 SSH 子进程 → Runner → 模拟 CLI → 实际 `LLMResponse` |
| 人格和历史 | system、user、assistant、当前 prompt 按顺序传递；原历史不被修改；不使用 AstrBot session ID 恢复 Cursor 会话 |
| 固定参数 | 模拟 CLI 校验精确 argv：Ask、指定 `mock-plan-model`、JSON、trust；无 force/yolo/resume |
| 环境/目录 | CLI 工作目录为空；不继承 AstrBot/其他服务 API Key、SSH agent、Node/Python 环境；生成拒绝权限配置 |
| 超时 | 假 CLI 睡眠，返回 `timeout`，不返回部分答案 |
| 未登录 | 缺少专用认证文件时不启动 CLI；CLI 报认证失效时返回 `not_logged_in` |
| CLI 失败 | 非零退出变成 `cli_failed`；原始错误中的聊天/密钥哨兵不出现在异常文本中 |
| 输出异常 | 无效 JSON、空回复、输出超限分别报错；不当成正常 assistant 回复 |
| 模型选择 | Auto、空值、不同 ID 拒绝；Runner 也检查固定模型 |
| 非文本 | 图片、音频、工具、工具历史均在调用前拒绝 |
| 停用/取消 | 取消待处理请求；后续请求报 Provider 已停止；子进程组清理测试检查延迟文件未被创建 |
| 热重载/共存 | 真实注册表结合小型 Manager 测试替身，检查只重建 Cursor 实例、保留选中 Cursor、保留 OAuth 占位实例身份 |

`mock-plan-model` 是**仅供模拟的假 ID**，不代表任何真实套餐模型。不要把它填到真实 Runner 的 `deploy/model_id`。生命周期测试没有运行真实 AstrBot WebUI 或原有 OAuth 插件，这部分仍需要 B 阶段验收。

可自行进行静态检查：

```bash
ruff check .
ruff format --check .
```

## B. 实际 AstrBot + 容器兼容性

这些仍由你在测试编排执行，结果请逐项记录：

- [ ] Runner 镜像能构建；服务启动不需要 privileged、Docker socket 或禁用 seccomp/AppArmor。
- [ ] Runner 没有发布公网/宿主端口，只有专用 internal 连接网络与必要的出口网络。
- [ ] 检查 Runner 的挂载列表，确认只有自己的密钥目录、模型文件、登录卷；没有 AstrBot 或其他服务目录。
- [ ] CLI 使用 UID 10001；工作目录为空；CLI HOME 位于本次临时目录；无 AstrBot、其他服务密钥或宿主 HOME。
- [ ] 你自行完成浏览器登录；没有设置 API Key；不能把账号信息、登录 URL 或 auth 文件放入验收记录。
- [ ] WebUI 出现 `cursor_cli` 模型来源；获取列表仅返回配置 ID，未调用模型。
- [ ] 关闭该会话的人格工具/MCP、图片和音频功能；选择明确的 Cursor 模型。
- [ ] 冷启动只产生一个 Cursor 模型实例；连续重载本插件不出现重复注册错误或重复实例。
- [ ] 重载后使用新配置，当前选中的模型保持正确；停用时待处理请求停止，停用后 Cursor 模型不可调用。
- [ ] 原 `astrbot_plugin_openai_oauth` 能继续加载、选中、重载和聊天；配置/凭据不被本插件修改。聊天会产生该服务的真实用量，是否执行由你决定。
- [ ] 对固定版本 CLI 核对 Ask/拒绝权限的实际效果。Ask 和 `--trust` 都不能单独当成安全隔离证明；本实现依赖容器挂载和环境白名单作文件隔离。
- [ ] 多个 QQ 会话独立历史；一个请求处理时第二个请求得到忙提示，不交叉复用 CLI 会话。
- [ ] 插件/Runner 日志没有聊天正文、登录 URL、令牌或原始错误。AstrBot 自身会话存储按你的配置管理。

如需要观察 CLI 行为，请只用无敏感信息的专门验收消息，并自行控制终端输出。不要向生产账号发送文件路径/密钥作为“测试数据”。

## C. 真实联通与套餐扣费（独立验收）

**A 阶段全绿、成功登录、模型列表可见，都不能代替本阶段。插件无法保证某个模型的套餐归属或强制 Cursor 账单分类。**

1. 在 Cursor 官方控制台查看当前套餐、可用模型及用量；先记录时间、套餐已用/剩余量、按量付费余额或收费明细。确认不会在套餐耗尽后意外使用你未授权的按量额度。
2. 自己从官方 `cursor-admin models` 获取准确模型 ID，并通过当前套餐说明/控制台确认它属于本次要验证的套餐范围。不要使用 Auto，也不要为了“跑通”临时换到其他模型。
3. 将这个 ID 同时填入 Runner 的 `deploy/model_id`、插件 `model_id` 和 AstrBot 原生模型。重建 Runner 并重载插件；记录 CLI 版本和 AstrBot 版本。
4. 发一条无敏感信息的短文本，验证 QQ 收到回复；再发一条需要前文的追问，验证 AstrBot 人格及历史的传递。每次都可能消耗额度。
5. 等待 Cursor 用量页面更新，按请求时间、模型和用量记录逐笔对照，确认变化发生在**套餐额度**，且没有额外按量收费或账单项目。若页面区分不清，应记录“计费归属无法确认”，不要勾通过。
6. 如请求失败或超时，也先查看用量再决定是否手动重试。插件没有自动重试，但服务端可能在返回前已处理请求。

验收记录模板：

| 字段 | 由你填写 |
| --- | --- |
| 插件提交 / AstrBot 版本 / CLI 版本 | 待填 |
| 明确模型 ID / Cursor 套餐 | 待填 |
| 请求前时间与套餐用量 | 待填 |
| 请求后时间与套餐用量 | 待填 |
| 按量收费/额外账单是否变化 | 待确认 |
| QQ 文本回复 / 人格 / 第二轮历史 | 待确认 |
| 套餐额度扣费结论 | 未验收 |

本文件初始状态保持“未验收”。只有你完成真实请求和 Cursor 用量页对照后，才可以在自己的验收记录中改为通过。
