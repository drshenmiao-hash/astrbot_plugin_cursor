# astrbot_plugin_cursor

AstrBot 原生 Cursor 聊天 Provider，后端只调用 **官方 Cursor CLI**。第一版纯文本，固定 Ask，模型必须手动指定。人格、多轮历史和 QQ 平台消息仍由 AstrBot 管理。

**交付状态：实现已提供，验证由使用者执行。未运行模拟 Provider 测试、容器构建、真实联通或扣费验收。不能据此认定 Cursor 套餐额度已验证可用。**

## 设计

```text
QQ → AstrBot（人格、历史）→ cursor_cli 原生 Provider
                             ↓ SSH stdin/stdout，强制固定命令
                       独立 Cursor CLI 容器
                             ↓ --print --mode ask --model 明确ID
                         官方 Cursor 服务
```

SSH 是到固定 CLI 运行器的进程管道，没有 HTTP 服务、OpenAI 兼容接口、Docker socket 或任意远程执行入口。这样无需给 AstrBot 容器开特权或额外内核权限，就能隔离 CLI 与 AstrBot 的文件系统及环境变量。专用容器是本插件的部署组件。

- 原生类型 `cursor_cli`，通过 AstrBot 的模型来源界面添加，可正常选择到 QQ 会话；不改核心，不代理或替换 OpenAI Provider。
- 插件热重载更新注册类，并只重载 Cursor 模型实例。卸载/停用取消请求并移除自己的实例；原有 OpenAI OAuth 配置、凭据、模型实例均不修改。
- `get_models()` 只返回手动配置的模型，不发网络请求。禁止 `auto`、`default`，不自动切换或重试，不轮换账号。
- 命令为 `cursor-agent --print --mode ask --model ID --output-format json --trust`。**不传 `--force`、`--yolo`、`--resume`、`--continue`。** `--trust` 仅信任本次新建的空工作目录；权限配置仍为 allowlist，并显式拒绝文件、Shell、WebFetch、MCP 工具。
- 聊天正文通过 stdin 传递，不进入命令行参数；每次创建空工作目录和临时 HOME，只复制浏览器登录的 access/refresh token。调用后只回存刷新后的凭据，清理 CLI 聊天文件，不用 CLI 管理多轮会话。
- CLI 环境使用固定白名单，不继承 AstrBot 的 API Key、代理、SSH agent、Node/Python 注入变量。CLI 容器只挂载自己的登录卷、专用 SSH 公私钥和模型 ID 文件，不挂载 AstrBot 数据、其他服务密钥或宿主目录。
- 插件及运行器不记录正文、凭据、argv、原始 stdout/stderr。固定错误提示；错误不会作为正常 assistant 回复返回。AstrBot 本身的消息记录和日志策略由其配置控制。
- 超时、取消和输出超限会杀死本地子进程组。远端也有独立的 CLI 超时；SSH 异常断开时，远端最迟到自身超时停止，网络中断前的请求仍可能计费。

## 版本与范围

源码对照：AstrBot **4.29.4**，提交 [`c5ec2392eb6f678602a48948e8079b888673df68`](https://github.com/AstrBotDevs/AstrBot/commit/c5ec2392eb6f678602a48948e8079b888673df68)，2026-11-14。用户表示使用“最新”；未读取你的线上容器，不能确认线上镜像与此提交相同。声明范围 `>=4.29.4,<5` 只是接口目标，不代表整个范围已实测。

CLI 固定到官方 **2026.11.13-eacade1** 的 Linux amd64 发行包并检查 SHA-256。v0.1 的容器配方为 amd64；ARM Ubuntu 需换成对应官方发行包并重新核验哈希和兼容性。

不支持图片、音频、AstrBot 工具调用、Cursor 工具工作流或账号管理面板。流式入口等待完成后返回一次最终结果。采样参数、token 限额等没有映射到 CLI；超过 256 KiB 的完整文本上下文会报错，由 AstrBot 压缩或重置，插件不偷偷截断历史。

人格以结构化文本 transcript 传入，**不是 Cursor 原生独立 system 消息**，因此人格遵循程度需要你验收。CLI 自身仍有其系统指令、服务端策略和内部行为。

## 安装

按 [Ubuntu + 1Panel + Docker 安装与升级](docs/INSTALL.md) 操作。文件包含：

- `main.py`、`cursor_plugin/`、`metadata.yaml`、`_conf_schema.json`：原生插件。
- `runner/`、`deploy/`：隔离运行器和 Compose 配方。
- `tests/`：[供你运行的模拟 CLI 测试与验收表](docs/ACCEPTANCE.md)。没有自动运行的 GitHub Actions。

仓库：<https://github.com/drshenmiao-hash/astrbot_plugin_cursor>。在 AstrBot 插件页使用 GitHub 仓库安装，或将整个仓库放进 `data/plugins/astrbot_plugin_cursor/`，同时按文档部署专用容器和只读 SSH 客户端卷。**仅安装插件不会自动安装 CLI 或登录账号。**

## 配置

在插件配置里设置下列字段，保存后重载本插件：

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `model_id` | 空 | 从官方 `agent models` 手动选定，并确认套餐内的明确 ID；不得使用 Auto |
| `ssh_host` | `cursor-cli` | 专用容器在隔离连接网络内的别名 |
| `ssh_port` | `2222` | 内部端口，不对宿主或公网发布 |
| `identity_file` | `/run/cursor-client/id_ed25519` | AstrBot 容器内的专用 SSH 私钥文件 |
| `known_hosts_file` | `/run/cursor-client/known_hosts` | 固定的已核验主机公钥文件 |
| `ssh_binary` | `/usr/bin/ssh` | OpenSSH 客户端绝对路径 |
| `timeout_seconds` | `120` | CLI 超时 1–600 秒，SSH 总预算多 15 秒 |

Runner 的 `deploy/model_id` 必须与插件 `model_id`、AstrBot 原生模型里的 `model` **完全一致**。服务端也检查这个模型 ID。源配置的 API Base、Key 均留空；这里不填写 Cursor 凭据或 OpenAI Key。

在 AstrBot **模型提供商/模型来源** 页新增 `cursor_cli`（Cursor CLI）来源，获取模型列表或手动添加同一个 ID 的聊天模型，然后在你的 QQ 会话/配置中选择它。仅获取列表不会调用 CLI。关闭该人格/会话的工具、MCP、图片和音频功能；携带这些内容的请求会明确报不支持。更改模型时同时更新以上三处并重启 Runner、重载插件；不会替你挑选模型。

## 验收与计费

所有验证留给你执行，见 [ACCEPTANCE.md](docs/ACCEPTANCE.md)。模拟 CLI 完全离线，不会证明登录有效、真实模型可用或套餐计费方式。

你需要自己完成 `cursor-admin login`。插件只接受浏览器订阅账号的登录凭据，拒绝凭据文件中的 API Key/Bedrock 凭据，不接受从 AstrBot 继承的 `CURSOR_API_KEY`。

**登录成功或拿到回复都不能证明扣的是套餐额度。** 模型是否在套餐内、具体 CLI 请求如何计费，由 Cursor 当前套餐和服务规则决定；本插件不能强制账单分类。首次真实请求前先核对 Cursor 用量页及按量付费设置，使用你确认在套餐内的明确模型，然后对照请求前后用量和时间记录。不允许把模拟结果填成真实通过。

## 参考

按官方 [插件使用文档](https://docs.astrbot.app/use/plugin.html) 进一步阅读了 [开发指南](https://docs.astrbot.app/dev/star/plugin-new.html) 和配置文档。

Provider 的注册、热重载思路参考 [astrbot_plugin_openai_oauth v2.5.0](https://github.com/wcqqq1214/astrbot_plugin_openai_oauth/tree/173f343b8ffff76ddee288ab71dfcf7c57a5c589) 并对照上述 AstrBot 源码实现；本项目独立实现，不导入或修改该插件。未移植其 OAuth、账号管理、HTTP 路由或 OpenAI 协议代码。

Cursor 依据官方 [Headless](https://cursor.com/docs/cli/headless)、[参数](https://cursor.com/docs/cli/reference/parameters)、[认证](https://cursor.com/docs/cli/reference/authentication)、[输出](https://cursor.com/docs/cli/reference/output-format)、[权限](https://cursor.com/docs/cli/reference/permissions) 文档；同时只读检查了固定发行包的 stdin、Linux 凭据路径及环境覆盖逻辑，没有运行官方 CLI。
