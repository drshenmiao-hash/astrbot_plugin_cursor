# 历史复用补丁

补丁基于用户提供的 Lumi 生产 traces 结论：普通聊天约 95% 的 system 不变，约 5% 在插件/AstrBot 话题中追加开发说明与 KB 卡片；时间戳、记忆、群昵称通常位于当前 user 末尾，旧轮内容保留在历史。该结论由用户提供，本仓库未读取或重放生产 traces。

本次只改历史比对和空增量，不调整进程、队列、SSH、隔离或部署方式。

## 行为

- 对话前缀排除所有 `role=system`。其它角色仍需完整前缀匹配，避免漏掉中间对话修改。assistant 内容仅在计算匹配 hash 时去除完整 `<think>` / `<thinking>` 块、末尾闭合标签和首尾空白；规则对应 AstrBot 4.29.4 的 OpenAI completion parser，不猜测删除未闭合的起始标签。user、原始回复和发送正文不改。
- system 单独保存“最近一次成功发送的状态”。变化时向原 session 发送一条 system 更新：追加只发新增后缀，替换发送新内容，删除明确撤回旧条目。次轮 system 为 `None` 且 contexts 没有其它 system 时，会发送撤回操作。
- system 更新和新 user 对话合并在同一次提示词中发送；没有新 user 时沿用最近成功回复，system 变化留待下一次新 user 输入发送，不额外发起一次生成。
- 同一请求重复（尚未含本次 assistant 回复）与确认历史重复（已含回复）都能复用结果。缓存命中发送字节为 0，不新建会话。缓存仅在内存中，按原会话作用域隔离。
- 没有新 user 且没有可用结果时返回空文本；初始请求完全没有 messages 的输入校验保持原样。失败和取消不缓存新结果或新 system 状态。

system 增量是对模型的提示词更新，无法物理删除既有上下文，也不保证模型严格遵循撤回。contexts 中的 `tool_calls` 仍不支持；历史窗口裁剪仍会新建会话。相同完整对话被视为重放；需要独立生成时应使用独立会话作用域。

## 验证记录

初版 `4bce09b` 没有补测；本次按后续要求补齐 `tests/test_history_reuse.py` 并实际执行：

```bash
python -m unittest -v tests.test_history_reuse
```

16 项全部通过，完整输出见 [HISTORY_REGRESSION_OUTPUT.txt](HISTORY_REGRESSION_OUTPUT.txt)。在归一化修复前，同一组测试有 3 项失败，分别复现 `<think>`、`<thinking>` 和末尾闭合标签剥离后误建会话；修复后全部通过。

测试覆盖：system 每轮时间戳、固定 system、assistant strip、原请求及含回复历史重放、次轮 system 为 None、system 记忆变化、当前 user 附加记忆、system 后缀追加、仅 system 变化的延迟发送；另覆盖 think 清理兼容、实际发送和回复原文保留、可见回答及 user 原文修改仍打断前缀。

测试通过实际 `normalize_messages → Engine.submit → 队列 → 缓存比对 → 渲染`，用内存 ACP 替身记录 session 和发送内容；检查复用、不重复生成、只发送差异、延迟 system 只更新一次及缓存回复的零发送字节。不依赖 Cursor、子进程、SSH、网络或 AstrBot 安装，不等于生产 traces 重放、官方 CLI 或套餐计费验收。

## 查看与回退

补丁在 `fix/persistent-cli-provider`，不自动合入 main。基础提交为 `e8e5ce237bb6428ec129910129820668fc988d25`。

```bash
git diff e8e5ce2..fix/persistent-cli-provider -- cursor_plugin/logic.py cursor_plugin/engine.py
```

回退可以在该分支 `git revert <本次补丁提交号>`，再重载 Cursor Provider；或恢复基础提交对应的完整插件目录。重载应清理旧实例及内存会话缓存，下一轮会重发完整历史；不需要修改登录卷、SSH 密钥或 AstrBot 核心。
