# 历史复用补丁

补丁基于用户提供的 Lumi 生产 traces 结论：普通聊天约 95% 的 system 不变，约 5% 在插件/AstrBot 话题中追加开发说明与 KB 卡片；时间戳、记忆、群昵称通常位于当前 user 末尾，旧轮内容保留在历史。该结论由用户提供，本仓库未读取或重放生产 traces。

本次只改历史比对和空增量，不调整进程、队列、SSH、隔离或部署方式。

## 行为

- 对话前缀排除所有 `role=system`。其它角色仍需完整前缀匹配，避免漏掉中间对话修改。assistant 内容仅在计算匹配 hash 时去除首尾空白；user 和发送正文不改。
- system 单独保存“最近一次成功发送的状态”。变化时向原 session 发送一条 system 更新：追加只发新增后缀，替换发送新内容，删除明确撤回旧条目。次轮 system 为 `None` 且 contexts 没有其它 system 时，会发送撤回操作。
- system 更新和新 user 对话合并在同一次提示词中发送；没有新 user 时沿用最近成功回复，system 变化留待下一次新 user 输入发送，不额外发起一次生成。
- 同一请求重复（尚未含本次 assistant 回复）与确认历史重复（已含回复）都能复用结果。缓存命中发送字节为 0，不新建会话。缓存仅在内存中，按原会话作用域隔离。
- 没有新 user 且没有可用结果时返回空文本；初始请求完全没有 messages 的输入校验保持原样。失败和取消不缓存新结果或新 system 状态。

system 增量是对模型的提示词更新，无法物理删除既有上下文，也不保证模型严格遵循撤回。contexts 中的 `tool_calls` 仍不支持；历史窗口裁剪仍会新建会话。相同完整对话被视为重放；需要独立生成时应使用独立会话作用域。

## 验证记录

按用户最新指示“不用补测了，已经测过了”，本次不新增或执行补测。只检查 Python 语法、Ruff 和补丁格式；不将历史的 23 项输出记作本次补丁的功能验证，不据此判断生产可用性。

## 查看与回退

补丁在 `fix/persistent-cli-provider`，不自动合入 main。基础提交为 `e8e5ce237bb6428ec129910129820668fc988d25`。

```bash
git diff e8e5ce2..fix/persistent-cli-provider -- cursor_plugin/logic.py cursor_plugin/engine.py
```

回退可以在该分支 `git revert <本次补丁提交号>`，再重载 Cursor Provider；或恢复基础提交对应的完整插件目录。重载应清理旧实例及内存会话缓存，下一轮会重发完整历史；不需要修改登录卷、SSH 密钥或 AstrBot 核心。
