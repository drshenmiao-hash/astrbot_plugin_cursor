# 安装与升级

在 AstrBot 插件页用仓库 URL 安装 v0.4.4，或下载 main 源码 ZIP 上传。接口基线为 AstrBot 4.29.4。先在独立测试编排和数据卷中验证联通与真实计费。[main 源码 ZIP](https://github.com/drshenmiao-hash/astrbot_plugin_cursor/archive/refs/heads/main.zip)

```bash
git clone --branch main https://github.com/drshenmiao-hash/astrbot_plugin_cursor.git /opt/astrbot-cursor
cd /opt/astrbot-cursor
```

需要官方 Cursor CLI，无需 Cursor 桌面版。模型 ID 必须明确指定并自行确认套餐范围；插件不自动换模型。AstrBot 已有的备用模型按框架原有规则接管。

## 选择运行方式

| 方式 | 适用条件 | CLI 位置 |
| --- | --- | --- |
| `local` | AstrBot 的执行环境内有 CLI、bubblewrap，且允许命名空间隔离 | AstrBot 容器内，或原生部署的同一台主机 |
| `ssh` | Docker/AppArmor 禁止本地命名空间，或希望凭据只留在专用容器 | 独立 Cursor CLI 容器 |
| `auto` | 自动选择 | 本地 CLI 和 bubblewrap 文件齐全时选 local，否则选 ssh；隔离失败不会裸跑 |

“宿主机装了 CLI”不等于 AstrBot 容器能直接调用它。Docker 用户可以先构建包含 CLI 的新测试镜像；已有可运行的本地环境无需 SSH。不要为本地模式开启 privileged、挂载宿主根目录或 Docker socket；隔离不可用时选择 ssh。

## 本地模式

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

在 1Panel 新建测试编排，使用此镜像，保留原启动命令和 UID，设置独立 AstrBot 数据卷。额外创建专用登录卷，挂载到 `/var/lib/cursor-login`，目录归 AstrBot 实际运行 UID 所有，不放其他服务文件。不要复用线上平台连接。

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

原生 Ubuntu 部署可使用已有的官方 CLI，填写其实际绝对路径，并安装 `bubblewrap`。使用 AstrBot 运行用户可读写的专用浏览器登录文件；不要指向日常开发工作区。CLI 必须保持官方完整发行目录（包含 `cursor-agent`、`node`、`index.js`）。

实际聊天创建独立空工作目录和临时 HOME。bubblewrap 只挂载 CLI、系统运行库、证书/DNS及这两个临时目录；不继承 AstrBot 环境变量，不挂载 AstrBot 数据或宿主 HOME。凭据刷新写回专用登录文件。隔离能力必须先诊断，不能仅靠 Ask 提示词作为文件边界。

## SSH 模式（按需）

无需在宿主机开放 SSH 端口。连接是 AstrBot 容器通过专用 Docker 网络直达 CLI 容器，不经过宿主机 SSH。

```bash
sudo python3 deploy/prepare_keys.py
docker network create --internal astrbot-cursor-link
docker compose -f deploy/compose.yaml build
docker compose -f deploy/compose.yaml up -d
```

网络已存在时跳过创建。密钥脚本遇到已有目录会停止，避免覆盖。非 root AstrBot 用户需要将 `deploy/client` 的所有者改为其实际 UID/GID；私钥权限保持 600。

1Panel 可在“容器 → 编排”中使用 `deploy/compose.yaml`，项目目录为 `/opt/astrbot-cursor/deploy`。Runner 只挂载服务端专用密钥与登录卷；不再挂载模型文件，不挂 AstrBot 数据，不发布宿主端口。

由你完成登录：

```bash
docker compose -f deploy/compose.yaml exec --user cursor cursor-cli /usr/local/bin/cursor-admin login
docker compose -f deploy/compose.yaml exec --user cursor cursor-cli /usr/local/bin/cursor-admin status
docker compose -f deploy/compose.yaml exec --user cursor cursor-cli /usr/local/bin/cursor-admin models
```

常驻 ACP 会持有登录卷锁。重新登录、退出或管理模型前，先在 AstrBot 禁用所有 Cursor 模型实例，等待进程停止；仅停用插件不保证已加载的模型被删除。不要删除锁文件绕过锁。

AstrBot 环境需有 OpenSSH 客户端；缺少时用 `deploy/Dockerfile.astrbot` 从原镜像构建新测试镜像。参考 `deploy/astrbot-service.example.yaml` 合并测试编排字段：只读挂载 `deploy/client` 到 `/run/cursor-client`，连接 `astrbot-cursor-link`，保留原网络、数据卷、启动命令和 UID。示例片段不能替换整个 AstrBot 编排。

配置 `transport=ssh`、`ssh_host=cursor-cli`、`ssh_port=2222`，使用默认专用私钥及 known_hosts 路径。修改主机名/端口时同步 known_hosts，保留主机校验。只需在插件填写模型，原生模型使用自动解析出的 ACP ID。

SSH 清理回执要求插件和运行器的协议版本匹配。可先用 `/cursor_doctor` 检查 `runner_cleanup_protocol`。旧运行器会报 `runner_cleanup_unsupported`。独立校验结束后，只有运行器确认本次 ACP 子进程树已退出并释放锁，插件才发布校验结果。传输中断或回执超时会报 `cleanup_unverified`，可安全检查远端状态后再重载。

SSH 镜像内置健康检查；未登录时显示 unhealthy 是预期结果。诊断分别返回客户端密钥、主机校验、SSH 版本、登录卷和 CLI 构建入口哈希。入口哈希校验不是对整个 Docker 镜像 digest 的远程证明。

## 安装插件与检查

在 AstrBot 插件页上传发布版源码 ZIP，或用仓库 URL 安装。手动安装时把源码放入 `data/plugins/astrbot_plugin_cursor/`，确保 `main.py` 和 `metadata.yaml` 直接位于插件根目录。默认分支和发布包都包含自动注册。

1. 在插件配置填写运行方式及明确 `model_id`，保存并重载。
2. 插件先连接已登录的官方 CLI，通过 ACP 查询并校验模型列表，不发送聊天提示词。校验成功后自动新增 `cursor_cli` 来源及原生模型；Key、API Base 留空，当前模型声明 text 和 image，由运行端及具体模型判断图片能力，新模型的 `id` 和 `model` 均使用解析后的 ACP modelId。空/auto/default 时只确保来源存在并提示，不创建模型；未登录、模型不存在或映射有歧义时明确报错，不自动换模型。
3. 以 AstrBot 管理员运行 `/cursor_doctor`。也可在插件根目录执行下列命令；配置 JSON 只包含插件设置，不包含令牌：

```bash
python -m cursor_plugin.diagnostics --config /path/to/plugin-config.json
```

环境检查、模拟进程脚本和真实请求验收见 [ACCEPTANCE.md](ACCEPTANCE.md)。WebUI“测试模型”和开启模型请求的验收脚本会消耗真实用量。

自动注册不改变当前默认模型。需要使用 Cursor 时由你在目标会话中选择；已有来源和相同模型会复用，不重复创建或改名。ID 冲突时不会覆盖其他提供商，见 [注册规则](REGISTRATION.md)。

## 升级与回滚

先记录旧插件提交、AstrBot 镜像、CLI 镜像和配置，备份专用密钥及登录卷。获取新提交后，在测试编排构建新镜像并执行环境脚本；通过后再由你更新正式编排和插件。更新 CLI 时同步版本与官方发行包 SHA，重新确认 ACP、Ask 和凭据格式。

升级会结束旧常驻进程并清除内存会话缓存。下一轮用 AstrBot 保存的历史文本和当前轮图片重建会话。回滚恢复旧插件和对应镜像再重载；不要执行 `docker compose down -v`，它会删除登录卷。OpenAI OAuth 插件配置及 AstrBot 的备用模型设置无需调整。
