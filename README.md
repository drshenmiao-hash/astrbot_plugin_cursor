# Cursor CLI for AstrBot

在 AstrBot 中使用 Cursor CLI 聊天，支持文本、图片和会话复用。适用 AstrBot 4.29.4 至 5.0 之前的版本。

## 开始使用

1. **一键安装插件。** 在 AstrBot WebUI 的插件页搜索 `astrbot_plugin_cursor`（Cursor CLI），也可用仓库 URL 安装。[GitHub 仓库](https://github.com/drshenmiao-hash/astrbot_plugin_cursor)
2. **安装 Cursor CLI。** 在 AstrBot 实际运行的主机或容器安装官方 CLI。已有 CLI 时直接检查服务能否找到它。[Cursor 官方安装指南](https://cursor.com/docs/cli/installation)
3. **选择运行方式。** 推荐优先使用本地 bubblewrap 隔离。在 **Cursor CLI → 用量与运行状态** 点 **检查环境**，查看 CLI、隔离、登录材料和所需组件。[运行方式与部署](docs/INSTALL.md#选择运行方式)
4. **登录 Cursor。** 由你在 CLI 所在环境完成登录。已有浏览器登录时填入可用的登录文件路径。[登录与配置](docs/INSTALL.md#登录文件与配置)
5. **填写模型 ID 并保存。** 插件用该 ID 校验并首次注册 Cursor 模型。以后可在 AstrBot 切换到已注册且 Cursor 账号可用的其他模型。[模型 ID 说明](docs/MODEL_IDS.md)
6. **检查连接并开始聊天。** ACP 连接和模型校验通过后，在 AstrBot 选择已启用的 Cursor 模型。**测试模型** 会发送真实请求，可能消耗 Cursor 额度。

本地直接运行需要管理员明确选择。该模式中的 CLI 拥有 AstrBot 运行用户的文件访问权限。临时 HOME、Ask 和工具限制不能代替系统级隔离。需要独立登录卷或运行环境时，可部署专用 [SSH Runner](docs/SSH_RUNNER.md)。

## 更新与用量

插件页出现新版提示时可更新。通过仓库 URL 安装的用户可参照安装指南。[安装与升级](docs/INSTALL.md#升级与回滚) · [GitHub Releases](https://github.com/drshenmiao-hash/astrbot_plugin_cursor/releases) · [变更日志](CHANGELOG.md)

**用量与运行状态** 显示本地请求统计与最近一次实际请求结果，也可导入 Cursor 官方用量 CSV。图片能力取决于 CLI 和所选模型。AstrBot 中的默认模型与备用模型由你管理。[用量说明](docs/LOCAL_USAGE.md) · [图片输入](docs/IMAGE_INPUT.md) · [会话复用](docs/HISTORY_PATCH.md)

本项目采用 [AGPL-3.0](LICENSE)。上游实现与署名见 [NOTICE](NOTICE)。
