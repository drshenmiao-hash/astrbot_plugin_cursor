# 安装与升级

适用 AstrBot 4.29.4 至 5.0 之前的版本。当前插件版本为 v0.4.6。

## 首次配置

1. **安装插件。** 在 AstrBot WebUI 的插件页搜索 `astrbot_plugin_cursor`（Cursor CLI），也可直接使用仓库 URL 安装。[GitHub 仓库](https://github.com/drshenmiao-hash/astrbot_plugin_cursor)
2. **选择运行方式。** 在插件设置查看下表。打开 **Cursor CLI → 用量与运行状态 → 检查环境**，可先检查候选方式，或检查已经保存的配置。需要更改 CLI 路径时可先保存路径，模型暂留空。
3. **准备 CLI 并登录。** 依照环境检查提示，由管理员在 CLI 实际运行的主机或容器安装组件并完成 Cursor 登录。已有登录可先检查登录文件。
4. **配置模型。** 填写套餐内的明确 Cursor 模型 ID 并保存。插件校验模型并注册到 AstrBot。[模型 ID 说明](MODEL_IDS.md)
5. **检查连接。** WebUI 分别显示系统依赖、登录材料、ACP 握手、模型确认和聊天基本条件。进入 AstrBot 选择已启用的 Cursor 模型即可聊天。需要实测时单独点 **测试模型**，该操作可能消耗 Cursor 额度。

## 选择运行方式

| 设置 | 适合场景 | 行为 |
| --- | --- | --- |
| 本地运行（bubblewrap 隔离，`local`） | 推荐用于原生 Linux 或已验证可运行 bubblewrap 的容器 | CLI 只获得隔离内的运行库、临时目录和专用登录材料。创建沙盒失败时停止。 |
| 本地运行（不使用 bubblewrap，`local_direct`） | 管理员愿意让 CLI 以 AstrBot 用户权限在同一环境运行 | 仍使用临时 HOME、空工作目录、必要环境变量及 Ask 模式。需要明确选择。 |
| SSH 专用 Runner（`ssh`） | 希望凭据留在专用容器，或当前环境无法使用 bubblewrap | 保留专用 SSH 密钥、固定主机公钥和登录卷。[SSH Runner 部署](SSH_RUNNER.md) |
| 自动检测（`auto`，原有默认） | 保留已安装实例的选择规则 | 找到本地官方 CLI 和 bubblewrap 时尝试隔离启动。所需组件缺失时尝试已配置的 SSH Runner。隔离启动失败会明确报错。自动检测绝不会选择直接运行。 |

**直接运行没有操作系统级文件隔离。** CLI 仍可访问 AstrBot 用户有权限访问的文件。若用 root 启动 AstrBot，CLI 也拥有相应的 root 权限。临时 HOME、Ask 模式、权限拒绝和空工作目录能减少常规配置继承，无法保证 CLI 永远读不到外部项目、指令、插件或其他服务凭据。建议采用低权限系统用户或权限受限的容器。不要开启 privileged，也不要挂载宿主根目录或 Docker Socket。

## 原生 Linux

在 AstrBot 实际运行用户的环境按 Cursor 官方指南安装。若使用隔离方式，由管理员通过系统包管理器安装 `bubblewrap`。[官方 CLI 安装指南](https://cursor.com/docs/cli/installation)

服务 PATH 中有 `agent` 或 `cursor-agent` 时，CLI 路径可留空。服务看不到终端的 PATH 时，填写 `agent` 或 `cursor-agent` 的绝对路径，例如 `/home/astrbot/.local/bin/agent`。保留官方发行目录中的 `cursor-agent`、`node`、`index.js` 和随包文件。

已有浏览器登录可填写该账户的文件路径，通常为 `~/.config/cursor/auth.json`。插件按照 AstrBot 服务用户解释 `~`。建议使用专用登录目录。若需新登录，使用 AstrBot 运行用户，在管理员终端执行：

```bash
mkdir -m 700 "$HOME/cursor-plugin-login"
HOME="$HOME/cursor-plugin-login" AGENT_CLI_CREDENTIAL_STORE=file NO_OPEN_BROWSER=1 \
  /实际路径/agent login
```

该命令适用于新建专用目录。把插件登录文件路径设为 `~/cursor-plugin-login/.config/cursor/auth.json`。在管理员终端运行同一环境下的 `agent status` 查看登录。WebUI 只报告检查状态。

## Docker 与 1Panel

CLI 要安装在 AstrBot 容器内，或者放在独立的 [SSH Runner](SSH_RUNNER.md) 中。宿主机上的 CLI 不会自动出现在容器中。在 1Panel 的容器编排中沿用原 AstrBot 启动命令和运行用户，并先用独立测试应用和数据卷验证。

`deploy/Dockerfile.astrbot-local` 为使用 apt 的 AstrBot 基础镜像安装 bubblewrap 和已校验的官方 CLI。固定 Linux amd64 CLI `2026.11.13-eacade1`，发行包 SHA-256：

```text
c02e4321f20d691ee03af94d29948dc3a81c6c34e7328da7bb1afade4c75812f
```

用自己当前镜像的实际 digest 构建新镜像。基础镜像使用非 root USER 时，在配方末尾恢复该 USER。

```bash
docker build -f deploy/Dockerfile.astrbot-local \
  --build-arg ASTRBOT_IMAGE='你当前的AstrBot镜像@sha256:实际digest' \
  -t astrbot-with-cursor-local:test .
```

Docker 或 1Panel 用户可新建测试编排，使用此镜像，保留原启动命令和 UID，设置独立 AstrBot 数据卷。额外创建专用登录卷，挂载到 `/var/lib/cursor-login`，目录归 AstrBot 实际运行 UID 所有，不放其他服务文件。不要复用线上平台连接。

由你在管理员终端登录；容器名和 UID 换成实际值：

```bash
docker exec -it --user '实际UID:实际GID' \
  -e HOME=/var/lib/cursor-login -e AGENT_CLI_CREDENTIAL_STORE=file \
  astrbot-test /opt/cursor/cursor-agent login
```

浏览器打开 CLI 显示的官方 URL。登录信息只保留在管理员终端。将末尾的 `login` 改成 `status` 或 `models` 可查看状态、模型；这些是用户管理操作，插件不提供账号面板。

管理端 `models` 的 ID 可能与 ACP 不同。插件以启动/重载时取得的 ACP `availableModels[].modelId` 为准；例如 `cursor-grok-4.6-high` 只在 ACP 确实包含 `grok-4.6` 时映射成功。也可直接填写 ACP ID 或唯一展示名，见 [模型 ID 说明](MODEL_IDS.md)。

插件配置：

```json
{
  "transport": "local",
  "cli_binary": "/opt/cursor/cursor-agent",
  "cursor_auth_path": "/var/lib/cursor-login/.config/cursor/auth.json",
  "model_id": "填写明确的套餐内模型ID"
}
```

上方镜像可用于两种本地方式。若 **检查环境** 显示容器无法实际建立 bubblewrap 沙盒，可选择 SSH Runner，或由管理员按前述风险说明显式启用直接运行。现有部署由管理员更新配置，插件不改容器权限。

## 登录文件与配置

两种本地方式通过锁串行使用配置中的登录文件。每个 CLI 进程只得到该浏览器登录文件的必要字段副本。直接模式要求文件归 AstrBot 运行用户所有且仅所有者可访问（通常为 `0600`），父目录也须由该用户管理且可写。官方 CLI 刷新令牌后，插件仅在源文件仍与读入时一致的情况下回写必要字段。手动重新登录前先结束使用同一登录文件的 Cursor 进程。插件不会执行登录或退出命令，也不会从 AstrBot 的服务环境传递其他 API Key。

`model_id` 用于首次校验与注册。若要切换模型，在 AstrBot 的模型提供商页面把套餐内的模型添加到 **Cursor CLI** 来源，再按会话或配置选择它。CLI 目录允许的明确 ID 和唯一别名才会生效。列表中没有 `grok-4.7` 时，选中该模型会报告不可用；可用性取决于你的 CLI 和 Cursor 账号。[模型 ID 说明](MODEL_IDS.md)

管理员也可运行 `/cursor_doctor`，或运行以下静态检查：

```bash
python -m cursor_plugin.diagnostics --config /path/to/plugin-config.json
```

配置文件只填写设置和路径。真实 CLI 和计费验证见 [验收指南](ACCEPTANCE.md)。

## 升级与回滚

先记录旧插件提交、AstrBot 镜像、CLI 镜像和配置，备份专用密钥及登录卷。获取新提交后，在测试编排构建新镜像并执行环境脚本；通过后再由你更新正式编排和插件。更新 CLI 时同步版本与官方发行包 SHA，重新确认 ACP、Ask 和凭据格式。

升级会结束旧常驻进程并清除内存会话缓存。下一轮用 AstrBot 保存的历史文本和当前轮图片重建会话。回滚恢复旧插件和对应镜像再重载；不要执行 `docker compose down -v`，它会删除登录卷。OpenAI OAuth 插件配置及 AstrBot 的备用模型设置无需调整。
