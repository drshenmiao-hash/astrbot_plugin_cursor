# Ubuntu + 1Panel + Docker 安装、登录与升级

以下是**由你执行**的步骤，本次交付没有在你的线上容器执行命令。先在独立测试编排完成验收，确认后再由你切换线上服务。不要把 CLI 装进现有运行中的 AstrBot 容器。

## 1. 准备独立目录和固定版本

在 Ubuntu 宿主机准备专用目录，例如 `/opt/astrbot-cursor`，从 GitHub 克隆本仓库。运行后续宿主命令的用户需有 Docker 管理权限。先在 1Panel 记录当前 AstrBot 镜像 tag/digest、USER、启动命令、环境变量、端口、网络和卷；保留原有 OpenAI OAuth 插件和数据卷，不修改它们。

```bash
git clone https://github.com/drshenmiao-hash/astrbot_plugin_cursor.git /opt/astrbot-cursor
cd /opt/astrbot-cursor
```

使用明确的发布 tag 或提交固定版本。当前源码声明目标 AstrBot 4.29.4；如果 1Panel 的“latest”实际是其他版本，先比较版本，不能凭镜像标签假定相同。

## 2. 准备独立 SSH 密钥

```bash
cd /opt/astrbot-cursor
sudo python3 deploy/prepare_keys.py
```

脚本仅生成该运行器自己的密钥，不连接 Cursor、不覆写已有密钥。`deploy/server/` 必须保持 root 所有：目录 755，`authorized_keys` 644，主机私钥 600。脚本生成的 known_hosts 直接来自本地创建的主机公钥，不用 `StrictHostKeyChecking=no` 或首次自动信任。

`deploy/client/` 只挂载给 AstrBot，目录 700、客户端私钥 600。若 AstrBot 以非 root UID 运行，将 **client 目录及内容**改为该 UID 所有，不要改 server 主机私钥的所有者。不要把两端的私钥混放，也不要提交它们。

```bash
# 将 1000:1000 换成实际 AstrBot 容器的 UID:GID；root 镜像无需这一条。
sudo chown -R 1000:1000 deploy/client
```

若更改 SSH 主机名或端口，需要对应修改 `client/known_hosts` 的 `[cursor-cli]:2222`，并手动核验公钥。不能关闭主机校验来解决连接问题。

## 3. 创建网络与构建 Runner

```bash
docker network create --internal astrbot-cursor-link
cp deploy/model_id.example deploy/model_id
```

若网络已存在，检查它是否是专用 internal 网络，再继续；不要重复创建。先把 `deploy/model_id` 改成准备验收的明确模型 ID，不能用占位符或 Auto。登录后可用 `cursor-admin models` 核对，然后同时更正插件配置。

在 1Panel **容器 → 编排**中新建独立的 `astrbot-cursor` 编排，使用 `deploy/compose.yaml`；保留实际文件路径和目录结构。Compose 使用相对路径，项目目录应为 `/opt/astrbot-cursor/deploy`，构建上下文应为上级目录。可直接在 Ubuntu 终端执行等价命令：

```bash
cd /opt/astrbot-cursor
docker compose -f deploy/compose.yaml build
docker compose -f deploy/compose.yaml up -d
```

仅新建专用 Runner。它不发布任何宿主端口，连接 internal 网络用于 AstrBot 传入请求，并用自己的 egress bridge 访问 Cursor。没有 host 网络、特权模式、宿主目录、AstrBot 数据卷或 Docker socket。仅 SSH daemon 使用指定的有限 capabilities，CLI 以 UID 10001 运行；保留 Docker 默认 seccomp/AppArmor。

固定 CLI：`2026.11.13-eacade1` / Linux x64，SHA-256：

```text
c02e4321f20d691ee03af94d29948dc3a81c6c34e7328da7bb1afade4c75812f
```

此值是交付时下载的官方包哈希，用于后续构建的一致性核对，非独立供应商签名。构建只解包官方 CLI，不执行安装脚本或模型请求。当前配方限定 amd64；ARM 宿主不要直接套用为“已适配”。

## 4. 你自行登录 Cursor

在你自己的终端执行：

```bash
docker compose -f deploy/compose.yaml exec --user cursor cursor-cli /usr/local/bin/cursor-admin login
```

打开命令在终端显示的官方登录 URL，用你自己的 Cursor 账号完成登录；不要将 URL、验证码或凭据贴到 QQ 或仓库。`NO_OPEN_BROWSER=1` 已由 helper 固定设置。持久化登录文件位于专用命名卷中的 `/var/lib/cursor-login/.config/cursor/auth.json`，不会挂载给 AstrBot。

```bash
docker compose -f deploy/compose.yaml exec --user cursor cursor-cli /usr/local/bin/cursor-admin status
docker compose -f deploy/compose.yaml exec --user cursor cursor-cli /usr/local/bin/cursor-admin models
```

这两个命令也由你执行，只用于查看登录状态和可用模型，不代表聊天联通/扣费验收通过。它们的输出可能含账号信息，不要公开。选择在你的套餐内、成本和规则已确认的模型，将准确 ID 写入 `deploy/model_id`。首次配置请使用编辑文件内容的方式；如果编辑器用原子替换导致 bind mount 仍指向旧 inode，重建 Runner 使其读取新文件。

```bash
docker compose -f deploy/compose.yaml up -d --force-recreate cursor-cli
```

这里的 `--force-recreate` 是 Docker Compose 参数，**没有传给 Cursor CLI**。

不要设置 `CURSOR_API_KEY` 或导入其他应用的 HOME。helper 只运行 login/logout/status/models，不提供账号管理面板。若官方 CLI 更改凭据存储格式，暂停升级并先核对固定版本，不要复制整个宿主 HOME 解决问题。

## 5. 准备新的 AstrBot 镜像与测试编排

如果你的 AstrBot 镜像已有 `/usr/bin/ssh`，可直接使用原镜像。否则从你的实际镜像构建一个新镜像：

```bash
docker build -f deploy/Dockerfile.astrbot \
  --build-arg ASTRBOT_IMAGE='你当前的AstrBot镜像@sha256:实际digest' \
  -t astrbot-with-cursor-ssh:4.29.4 .
```

此配方适用使用 apt 的 AstrBot 镜像。如果原镜像为非 root USER，需要在你自己的配方尾部恢复原 USER；不要因为安装客户端而让 AstrBot 长期改用 root。不同基础镜像需按其包管理器调整，不能在在线容器里 `apt install`。

在 1Panel 复制现有 AstrBot 编排到独立的测试编排，使用**独立数据卷/已有数据的测试副本**，不要让两个 AstrBot 同时写同一线上数据卷，也不要让测试实例重复连接生产 QQ 账号。合并 `deploy/astrbot-service.example.yaml` 的字段：

1. 添加只读挂载 `/opt/astrbot-cursor/deploy/client:/run/cursor-client:ro`。
2. 连接外部 `astrbot-cursor-link` 网络，并保留原有网络。
3. 仅在需要时使用新建的 SSH 客户端镜像，保留原有启动命令、平台配置和 OAuth 插件。

示例文件是需要合并的片段，不能单独当成完整 AstrBot 编排覆盖线上配置。

## 6. 安装插件和创建原生模型

在测试 AstrBot WebUI 的插件页通过本仓库地址安装；也可将仓库放在测试实例的 `data/plugins/astrbot_plugin_cursor/`。不要把 Runner 的密钥目录复制进插件数据目录。无需额外 Python 依赖。

1. 插件配置填写 `model_id`，须与 Runner 模型文件相同；检查 SSH 文件路径并保存、重载插件。
2. 在模型提供商/模型来源页添加 `cursor_cli`（Cursor CLI）来源，Key/API Base 留空。
3. 获取列表或手动添加该明确模型 ID，启用聊天模型；不使用 Auto 或备用模型。
4. 在相应 AstrBot 会话/配置中选择这个模型，保留原人格与历史管理；将该人格的工具、MCP、图像和音频功能关闭。
5. 按 [验收说明](ACCEPTANCE.md) 由你运行测试。WebUI 的“测试模型”按钮会发送真实请求，可能计费，请到真实验收阶段再按。

不自动新增或迁移来源，不覆盖其他 Provider 配置。首次启动由 AstrBot 原生加载配置；热重载会替换 Cursor 注册类并重建自己的模型实例。停用后实例停止，已有会话绑定仍保留，请你按需换回原模型；插件不替你改变 QQ 的模型选择。

## 7. 升级与回滚

1. 先备份 AstrBot 配置/数据、专用 SSH 密钥及 `cursor-login` 卷，按凭据级别保护备份。
2. 记录旧插件提交和 Runner 镜像 ID；拉取并审阅新发布版本，在测试编排中构建新的 Runner 镜像。
3. 若更新 CLI，修改固定版本与从官方包核对得到的 SHA-256。不要在运行中的容器执行 `agent update`。同时核对 Ask、stdin、输出格式、凭据路径和权限配置。
4. 按验收表完成模拟、兼容性和真实计费检查。之后由你在维护时段更新生产编排，重建 Runner，并在 AstrBot 更新/重载本插件。
5. 保留原 OAuth 插件配置；不要删除 `cursor-login` 卷，不要执行带 `-v` 的 `compose down`，除非你明确要清除登录。

回滚时恢复旧插件版本和旧 Runner 镜像，重建该服务并重载本插件；如果不再使用，先把 QQ 会话切回原 Provider，再停用插件、停止专用服务。CLI/网络失败不会自动回退到其他模型。

## 常见错误

| 提示 | 处理 |
| --- | --- |
| 配置无效 | 检查绝对路径、权限、模型文件，确认凭据为本容器的浏览器登录而非 API Key |
| 无法连接专用容器 | 检查 internal 网络、服务名、2222 端口、客户端 UID 和 known_hosts；不关闭主机校验 |
| 未登录/已失效 | 由你重新执行本容器的 `cursor-admin login` |
| CLI 调用失败 | 核对明确模型权限、套餐余额、网络及团队 CLI 使用策略；不会自动换模型 |
| 返回格式无效 | 检查是否仍使用固定的官方 CLI 包，禁止包装脚本往 stdout 打印额外文本 |
| 正在处理其他请求 | Runner 串行，忙时直接拒绝且不发模型请求；可稍后手动再试 |
| 超时 | 先检查 Cursor 用量；插件不重试，超时不等于未计费 |

不要为了排错启用聊天正文或凭据的调试日志。若要在终端直接运行官方 CLI 诊断，请自行管理可能出现的敏感输出。
