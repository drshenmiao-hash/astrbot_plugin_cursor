# system 时钟噪音

system 的原文仍保留在 AstrBot 的请求中；首次完整提示词保持原样。历史对话 hash 的规则不变，system 仍不参与对话前缀 hash。

计算 system_delta 时，仅忽略明确的时钟元数据行。纯时钟变化不产生 replace/append，也不重建 session。正常增量请求在文本头部携带最小的 `Current clock fields for this request`，使用本轮提取的原始时间字段，并说明只覆盖此前时钟值；JSON 对话部分只发送新对话。这样模型仍能获得最新时间，而不必接收整段人设。

人设、规则、工具说明、记忆等变化仍会检出；replace 使用完整的新原文，append 使用对应的原文后缀，不把比对用的文本写回 AstrBot。没有新 user 输入时沿用缓存，不为时钟单独发模型请求；下一次新 user 输入使用该轮最新时钟。

## 保守识别范围

支持独占一行、带明确标签的时间字段，例如：

- `时间：2026-11-16 04:51`、`时刻：04:52:30`。
- `Current time: 2026-11-16T04:52:01Z`。
- `系统时间：2026/11/16 04:52:00 UTC+08:00`。
- `当前日期：2026年11月17日`、点分隔日期、英文标签。
- `Time zone: Europe/London`、`时区：UTC+08:00`。
- 方括号、Markdown 加粗标签，以及分号/竖线分隔的多个纯时钟字段。

日期和时刻校验有效性；IANA 时区使用本地时区数据校验，不发网络请求。若环境缺少相关时区数据，就保留原文。

不从任意正文抓取数字或日期。不剥离会议/截止时间、订单/规则编号、预算、版本号，不猜测 CST 等歧义时区或非法时间。时间与实质规则混写、格式不明确时整行保留；这种保守情况可能仍产生 system 增量，避免误删实质指令。纯时钟 system 条目不参与实质 system 编号，时钟通过独立字段更新。

system 撤回/替换仍是发给模型的说明，不能物理清空已有上下文。本次不改变默认/备用模型、注册配置、队列或会话缓存 key。

## 实际回归

```bash
python -m unittest tests.test_system_clock tests.test_usage tests.test_models tests.test_registration tests.test_history_reuse
```

79 项全部通过，包括 10 项时钟回归、14 项本地统计、24 项模型解析、15 项注册和 16 项历史复用。原有 `test_system_timestamp_changes_every_turn` 已改为断言没有 system 增量、仍复用 session、实际发送仍包含本轮时间且不含整段人设。

完整输出见 [SYSTEM_CLOCK_TEST_OUTPUT.txt](SYSTEM_CLOCK_TEST_OUTPUT.txt)。测试经过真实规范化、Engine 和发送文本渲染，使用内存 ACP/ProviderManager 替身；没有连接真实 Cursor 或生产环境。默认模型保护仍由同一组合中的注册及统计测试检查。
