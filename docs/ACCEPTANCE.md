# 检查与验收

只将实际执行的项目记为通过。历史复用补丁已补齐并执行 16 项内存回归测试，见 [补丁说明和输出](HISTORY_PATCH.md)。下面的纯逻辑脚本也重新执行通过。环境脚本交给部署者运行。内存替身和模拟 ACP 都不代表官方 CLI、AstrBot 实机联动或套餐计费通过。

自动注册补丁另新增 15 项回归，和上述 16 项合跑共 31 项通过。命令为 `python -m unittest -v tests.test_registration tests.test_history_reuse`，完整输出见 [REGISTRATION_TEST_OUTPUT.txt](REGISTRATION_TEST_OUTPUT.txt)，保护范围及测试替身边界见 [REGISTRATION.md](REGISTRATION.md)。

## A. 已执行的纯逻辑

在插件根目录运行，Python 3.10+，无需 AstrBot、网络或 CLI：

```bash
python tests/check_logic.py
```

脚本直接 import `cursor_plugin.logic`，打印输入、实际输出并断言：

- 七组历史：新增、中间修改、末尾标点、缩短到一条、单条相同、首次与空历史。核对长度、对话前缀 hash 和末两条结果；system 独立更新及成功请求重放的实际 Engine 行为由上述回归测试覆盖。
- 三组队列：同会话序号 3/1/2 到达后排为 1/2/3、同会话已执行时阻塞、没有序号时保持到达顺序。
- 十三组错误文本分类：主机公钥、客户端密钥、隔离、认证、排队、网络、模型、限流、用量、上下文、协议、超时、未知失败。

当前实际结果为 `RESULT: 23 pure-logic checks PASS; network/process/SSH calls: 0`。这证明这些规则函数在给定输入下的结果，不证明实际异步调度或子进程行为。

完整实际输出见 [LOGIC_OUTPUT.txt](LOGIC_OUTPUT.txt)。

## B. 由你运行的单一环境脚本

脚本为 `tests/check_environment.py`。五项依次是 PID 复用、同会话并发、取消后断线重连、每轮发送字节、依赖诊断。

每项打印 `PASS`、`FAIL` 或 `SKIP`，紧随 `RAW` JSON，含实际回复、PID、元数据或异常堆栈。依赖探针保留退出码、标准输出和标准错误；不打印认证帧、登录文件内容或 CLI 原始聊天错误。无法观测实际 CLI PID 时明确 SKIP，不用启动器 PID 假冒。异常或断言失败返回非零；缺依赖、未配置、未授权真实模型调用时 SKIP 并说明缺什么，SKIP 不是通过。

### 模拟 ACP 子进程

```bash
python tests/check_environment.py --mock
```

使用 `tests/mock_acp.py`，无需 Cursor 登录或网络；模拟登录、模型列表及回复。会创建真正的本地子进程以检查生命周期，不能归入 A 的纯函数检查。此命令尚未由作者运行。

### 真实环境

复制 `tests/environment_config.example.json` 到测试实例，填写该实例实际的路径、运行方式及明确套餐内模型 ID。也可导出插件设置为 JSON；文件不放令牌。

先只诊断，不发送提示词：

```bash
python tests/check_environment.py --config /path/to/plugin-config.json
```

确认登录和模型后，显式开启真实请求：

```bash
python tests/check_environment.py --config /path/to/plugin-config.json --allow-model-requests
```

从与 AstrBot 相同的容器、UID、挂载、网络和 Python 环境执行。不要与正在服务的 Cursor Provider 共享登录锁；使用独立测试实例或暂时禁用该实例的 Cursor 模型。

| 项目 | 脚本检查 |
| --- | --- |
| PID | 连续两轮的实际 CLI PID 一致；本地通过 `/proc` 查找，SSH 由固定运行器报告 |
| 同会话并发 | 同批提交序号 3、1、2，检查完成顺序 1、2、3，失败请求不会被当成回复 |
| 取消后重连 | 取消活动提示词、等待任务清理，再主动断开连接，确认新 PID 能返回回复；太快完成而未覆盖取消时 SKIP |
| 字节 | 两轮打印完整输入、增量文本、实际 ACP 提示帧和输出字节；第二轮等于预期增量，且 `reused=true` |
| 诊断 | 本地 CLI/隔离/登录；SSH 密钥/主机/版本/登录卷/入口哈希分别报告 |

同会话重排只覆盖尚未执行、在 25 ms 聚合窗口内且都带序号的请求；不保证 QQ 消息自身的时间顺序。脚本直接调用 Engine，并不覆盖 AstrBot WebUI、QQ 平台、消息事件上下文或备用模型切换。

## C. 框架与计费的独立验收

以下必须在你实际的 AstrBot 和 Cursor 账号中核对：

- 原生来源注册、冷启动、连续重载、模型选择和终止清理；原 OpenAI OAuth 插件仍可使用，原有备用模型能按框架规则接管。
- 两个 QQ 会话隔离、人格与历史正确；观察实际事件环境能显示排队及超时报错；没有稳定会话标识时应记录 `no_stable_session_key`。
- Ask 固定、客户端文件/终端能力关闭；通过测试环境检查 CLI 看不到 AstrBot 数据与其他服务密钥。A 不覆盖隔离安全边界。
- 日志只有元数据，无聊天正文或凭据。环境脚本的 RAW 回复只用于管理员验收，不应粘贴到公开日志。

真实计费不能从发送字节或成功回复推断。测试前在 [Cursor 用量页](https://cursor.com/dashboard/usage) 记录时间、套餐用量与额外收费；测试后等待更新，按时间和明确模型核对是否扣套餐。取消、失败和超时也可能产生用量，脚本不会自动重试。CLI 会话复用减少重复传输，但没有证明历史 token 免费、缓存必定命中或费用减半。

| 记录项 | 待填结果 |
| --- | --- |
| 插件提交 / AstrBot / CLI 版本 | |
| A 输出 / B 每项 PASS、FAIL、SKIP 及 RAW | |
| 真实 QQ、Provider 重载、备用模型 | |
| 明确模型 / 套餐 / 请求时间 | |
| 用量页前后套餐扣量 / 额外收费 | |
| 最终结论；无法区分时写“无法确认” | |
