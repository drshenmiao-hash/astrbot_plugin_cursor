# 安装与升级

适用于 Ubuntu + Docker Compose，也可通过 1Panel 管理编排。建议先使用独立的 AstrBot 测试实例；测试实例应使用单独的数据卷和消息平台连接。

## 1. 获取插件源码

```bash
git clone https://github.com/drshenmiao-hash/astrbot_plugin_cursor.git /opt/astrbot-cursor
cd /opt/astrbot-cursor
```

部署账号需要 Docker 管理权限。当前配方面向 Linux amd64 和 AstrBot 4.29.4。部署前记录现有 AstrBot 的镜像版本、运行 UID、启动命令、网络和卷，以便合并配置及回滚。

## 2. 生成专用 SSH 密钥

```bash
sudo python3 deploy/prepare_keys.py
```

脚本创建两组文件，已有目录时会停止，避免覆盖密钥：

| 目录 | 用途 | 权限 |
| --- | --- | --- |
| `deploy/server/` | Runner 主机密钥和客户端公钥 | root 所有，目录 755，主机私钥 600，`authorized_keys` 644 |
| `deploy/client/` | AstrBot 客户端私钥和 `known_hosts` | 目录 700，私钥 600，所有者为 AstrBot 运行 UID |

如果 AstrBot 使用非 root 用户，例如 UID/GID 为 `1000:1000`：

```bash
sudo chown -R 1000:1000 deploy/client
```

`known_hosts` 由本地生成的主机公钥写入，默认对应 `[cursor-cli]:2222`。修改主机名或端口时需同步更新该项，保留主机公钥校验。

## 3. 启动 Cursor CLI 容器

```bash
docker network create --internal astrbot-cursor-link
cp deploy/model_id.example deploy/model_id
docker compose -f deploy/compose.yaml build
docker compose -f deploy/compose.yaml up -d
```

网络已存在时可跳过创建命令。`deploy/model_id` 暂时保留占位内容，登录并选定模型后再填写；配置完成前不发送聊天请求。

在 1Panel 的“容器 → 编排”中，可用 `deploy/compose.yaml` 创建独立编排。项目目录设为 `/opt/astrbot-cursor/deploy`，构建上下文为上级目录，保留文件中的相对路径。

Runner 不发布宿主端口。它通过专用 internal 网络接收 AstrBot 请求，通过独立出口网络访问 Cursor。挂载仅包含专用 SSH 密钥目录、模型文件和登录卷，无需特权模式或 Docker socket。

CLI 固定版本为 `2026.11.13-eacade1`，Linux x64 发行包的 SHA-256 为：

```text
c02e4321f20d691ee03af94d29948dc3a81c6c34e7328da7bb1afade4c75812f
```

构建时下载并核对发行包，不执行官方安装脚本。ARM 环境需要改用对应发行包并重新核对哈希。

## 4. 登录并选择模型

```bash
docker compose -f deploy/compose.yaml exec --user cursor cursor-cli /usr/local/bin/cursor-admin login
```

在浏览器打开终端显示的官方登录 URL，完成个人 Cursor 账号登录。登录 URL、验证码和凭据应保留在管理员终端内。

查看状态和可用模型：

```bash
docker compose -f deploy/compose.yaml exec --user cursor cursor-cli /usr/local/bin/cursor-admin status
docker compose -f deploy/compose.yaml exec --user cursor cursor-cli /usr/local/bin/cursor-admin models
```

选择已确认属于套餐范围的明确模型 ID，将其写入 `deploy/model_id`，然后重建容器以重新加载挂载文件：

```bash
docker compose -f deploy/compose.yaml up -d --force-recreate cursor-cli
```

登录保存在专用命名卷的 `/var/lib/cursor-login/.config/cursor/auth.json`。该卷不挂载到 AstrBot。普通聊天使用临时 HOME，只回存刷新后的登录凭据，不保存 CLI 聊天历史。

## 5. 为 AstrBot 配置 SSH 连接

AstrBot 容器需要 `/usr/bin/ssh`。原镜像已包含 OpenSSH 客户端时可直接使用；否则从原镜像构建新镜像：

```bash
docker build -f deploy/Dockerfile.astrbot \
  --build-arg ASTRBOT_IMAGE='原AstrBot镜像@sha256:实际digest' \
  -t astrbot-with-cursor-ssh:4.29.4 .
```

该 Dockerfile 适用于使用 apt 的基础镜像。原镜像使用非 root USER 时，应在配方末尾恢复原 USER。依赖安装在构建阶段完成，无需修改运行中的容器。

将 [AstrBot 编排示例](../deploy/astrbot-service.example.yaml) 的字段合并到现有编排：

1. 添加只读挂载 `/opt/astrbot-cursor/deploy/client:/run/cursor-client:ro`。
2. 连接外部网络 `astrbot-cursor-link`，保留原有网络和数据卷。
3. 如有需要，换用包含 OpenSSH 客户端的新镜像，保留原启动命令和运行 UID。

示例是配置片段，不能作为完整 AstrBot 编排直接替换现有配置。

## 6. 安装插件并添加模型

在 AstrBot 插件页使用以下仓库地址安装：

```text
https://github.com/drshenmiao-hash/astrbot_plugin_cursor
```

也可将源码放到 `data/plugins/astrbot_plugin_cursor/`。插件没有额外 Python 依赖。

1. 打开插件配置，填写 `model_id`，与 Runner 的 `deploy/model_id` 保持一致。
2. 检查 SSH 文件路径，保存并重载插件。
3. 在模型来源页新增 `cursor_cli`（Cursor CLI），Key 和 API Base 留空。
4. 获取列表或手动添加同一模型 ID 的聊天模型。
5. 在目标会话或配置中选择该模型，关闭工具、MCP、图片和音频功能。

模型列表来自插件配置，不会调用 Cursor。WebUI 的“测试模型”按钮会发送真实请求，可能消耗额度。

插件不修改 OpenAI OAuth 的配置或凭据。测试步骤见 [测试与验收指南](ACCEPTANCE.md)。

## 升级与回滚

升级前备份 AstrBot 配置、专用密钥和 `cursor-login` 卷，并记录插件提交及 Runner 镜像 ID。

1. 获取新版本源码，在测试编排中构建 Runner。
2. 如果更新 CLI，同步修改固定版本和官方发行包哈希，核对 Ask、输入输出及凭据存储格式。
3. 完成检查后，更新生产编排中的 Runner 镜像，并更新、重载 AstrBot 插件。

回滚时恢复旧插件和 Runner 镜像，再重载插件。登录卷可继续保留；`docker compose down -v` 会删除命名卷并清除登录。

停用插件前，可先将会话切回其他模型。插件会取消自己的请求，不会自动改变会话绑定或切换到备用模型。

## 故障排查

| 提示 | 检查项 |
| --- | --- |
| 配置无效 | 路径、文件权限、明确模型 ID，以及浏览器登录凭据 |
| 无法连接专用容器 | 网络、`cursor-cli` 别名、2222 端口、客户端 UID 和 `known_hosts` |
| 未登录或登录失效 | 重新执行 `cursor-admin login` |
| CLI 调用失败 | 指定模型权限、套餐余额、网络和团队 CLI 策略 |
| 返回格式无效 | 官方 CLI 固定版本及 JSON 输出格式 |
| 正在处理其他请求 | Runner 串行处理，等待前一请求结束后重试 |
| 超时 | 先核对 Cursor 用量，再决定是否重试 |

插件隐藏原始 CLI 错误，避免日志包含正文或凭据。终端诊断输出也应按敏感信息管理。
