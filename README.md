# Cursor CLI for AstrBot

在 AstrBot 中使用 Cursor CLI 作为聊天模型提供商，支持文本、图片输入和会话复用。人格和聊天历史由 AstrBot 管理。插件适用于 AstrBot 4.29.4 至 5.0 之前的版本。

## 安装与配置

1. **准备 Cursor CLI。** 在运行 CLI 的环境里用官方 CLI 登录 Cursor。本地模式要求 AstrBot 的运行环境中有 CLI 和可用的 `bubblewrap`。如果容器不支持本地隔离，可使用专用 SSH 运行器。按你的部署方式参照[安装指南](docs/INSTALL.md#选择运行方式)。
2. **安装插件。** 在 AstrBot WebUI 的插件页搜索 `astrbot_plugin_cursor`（Cursor CLI）。如果你的市场源未收录，可在同一页面通过仓库 URL 安装：`https://github.com/drshenmiao-hash/astrbot_plugin_cursor`。
3. **配置模型。** 在插件页打开 Cursor CLI 的配置，按部署方式填写 `transport` 和相应的 CLI 或 SSH 设置，再填写 `model_id`。在运行端用 CLI 的 `models` 命令查看候选模型。插件最终按 Cursor ACP 返回的模型 ID 校验，也接受可唯一匹配的显示名称。选一个账号可用的具体模型，避免填写 `auto` 或 `default`。[模型 ID 说明](docs/MODEL_IDS.md)
4. **保存并重载插件。** 校验通过后，插件会在 AstrBot 中注册 Cursor 来源和对应模型。在目标聊天会话里选中这个模型，即可开始使用。

## 检查连接与用量

在 AstrBot WebUI 打开 **Cursor CLI → 用量与运行状态**。

- **检查连接**查看 Runner、CLI、ACP 握手和认证各自的状态，无模型请求。
- **测试模型**向当前配置的模型发一条真实请求，可能消耗 Cursor 额度。
- **最近一次实际请求**显示正常聊天的最近结果及失败阶段。用量页还提供本地请求统计，可导入 Cursor 官方用量 CSV 查看账号 Token 汇总。[用量和诊断说明](docs/LOCAL_USAGE.md)

## 更新

通过 AstrBot 插件市场安装的用户，可在插件页有新版提示时更新。若通过仓库 URL 安装，请参照[安装与升级](docs/INSTALL.md#升级与回滚)。正式版本及变更见 [GitHub Releases](https://github.com/drshenmiao-hash/astrbot_plugin_cursor/releases)和[变更日志](CHANGELOG.md)。

## 功能与限制

- Cursor CLI 以 Ask 模式运行，使用插件配置的模型。图片能否使用取决于具体模型和 CLI 运行端。音频和工具调用暂不支持。[图片输入说明](docs/IMAGE_INPUT.md)
- 插件复用连续对话的 Cursor 会话，以减少重复传输。上下文变化或进程重启时会按需重建会话，实际用量以 [Cursor 用量页](https://cursor.com/dashboard/usage)为准。[会话复用说明](docs/HISTORY_PATCH.md)
- 插件注册模型后，由你在 AstrBot 里选择何时使用。现有默认模型和备用模型的设置会保留。[自动注册说明](docs/REGISTRATION.md)

部署细节见[安装指南](docs/INSTALL.md)，开发与验证见[验收指南](docs/ACCEPTANCE.md)。本项目采用 [AGPL-3.0](LICENSE)，上游实现与署名见 [NOTICE](NOTICE)。
