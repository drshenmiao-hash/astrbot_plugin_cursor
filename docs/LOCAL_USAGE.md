# 插件本地统计

统计来自本插件的 `request_finished` 事件，**不是 Cursor 账单、套餐余额、token 数或官方消耗**。统计和查询均不访问 Cursor 账单接口，不增加模型请求，不改变模型并发、请求队列或会话缓存。

## 管理员命令

```text
/cursor_usage
/cursor_usage day
/cursor_usage day 2026-11-16
/cursor_usage session
/cursor_usage session <匿名会话标识>
/cursor_usage model
/cursor_usage model grok-4.6
```

仅 AstrBot 管理员可使用。默认查看总计；分组每次最多显示 20 组，可按完整值筛选。日期使用 UTC。各组显示请求数、成功/失败数及结果码分布，耗时/排队总量和均值，input/sent/wire/output 字节总量，以及进程/会话复用次数。

请求数包含成功、失败、队列超时、取消和缓存命中。缓存命中不是新的 Cursor 模型调用。模型分组采用 ACP 已确认并实际用于发送提示词的 modelId；排队失败、缓存等未记录到 ACP 发送的请求单独分组，不把配置中的管理端 ID 冒充实际 modelId。该分组也不能证明官方没有产生费用。

模型列表发现、统计查询及其它 Provider 的事件不计数。启动之前只有日志、没有落库的历史不会被自动补记。

## 存储与隐私

数据仅写入 AstrBot 的 `data/plugin_data/astrbot_plugin_cursor/`：

- `usage.sqlite3`，及 SQLite 自己的 WAL/SHM 文件。
- `usage-session.key`，用于生成稳定的匿名会话标识，正常重启后仍可按同一匿名标识汇总。

不修改 cmd_config、默认/备用模型或 OAuth/DeepSeek 配置。数据库只记录上述元数据，不记录聊天正文、回复正文、凭据或原始 QQ 标识。统计会话标识与 Engine 的缓存 key 分开，不改变原有会话复用逻辑。

单独的串行存储线程异步提交 SQLite 写入，模型 worker 数不变。管理员查询等待已经提交的记录落盘；正常退出等待写入，强制终止或磁盘故障可能缺少尚未提交的记录。写入错误只打印固定错误码，不阻断模型结果交付；查询会报告写入异常或数据可能不完整。SQLite 统计不会自动清理，备份时保留数据库及匿名 key。

## 回归

```bash
python -m unittest tests.test_usage tests.test_models tests.test_registration tests.test_history_reuse
```

69 项通过，其中 14 项本地统计回归，完整实际输出见 [LOCAL_USAGE_TEST_OUTPUT.txt](LOCAL_USAGE_TEST_OUTPUT.txt)。覆盖成功/失败、全部元数据、按模型/UTC 日/会话汇总、重启持久化、匿名标识稳定、请求去重、只写专用目录、不存正文/凭据、默认配置不变、缓存不额外调用模型、写入失败不影响回复，以及失败 ACP 请求保留实际 modelId。

测试使用临时目录和内存 ACP/ProviderManager 替身，不连接真实 Cursor；管理员权限装饰器另有静态检查，尚未验证实际 AstrBot/QQ 命令链路。
