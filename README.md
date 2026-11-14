# astrbot_plugin_cursor

在 AstrBot 中使用官方 Cursor CLI 进行纯文本聊天。人格和多轮历史由 AstrBot 管理，插件将文本上下文交给 Cursor，再把回复返回到 QQ 等消息平台。

> 预览版本：已完成静态检查，模拟测试、容器运行、真实联通和套餐计费尚未验收。

## 功能

- 注册原生 `cursor_cli` Provider，可在 AstrBot 中添加和选择模型。
- 固定使用 Ask 模式，手动指定模型 ID，不自动换模型或重试。
- CLI 在专用容器中运行，每次调用使用空工作目录和临时 HOME。
- 通过 stdin 传递正文，过滤子进程环境变量，隐藏原始错误输出。
- 支持插件重载和请求取消，无需修改 AstrBot 核心或原有 OpenAI OAuth 插件。

目前只支持纯文本。图片、音频、工具调用和工具历史会被拒绝；流式接口一次返回最终回复。

## 运行方式

```text
QQ → AstrBot → Cursor Provider → SSH → 专用容器中的官方 Cursor CLI
```

SSH 仅用于向固定运行器传递请求，不提供交互式 Shell 或端口转发。项目没有 OpenAI 兼容 HTTP 桥接，也不需要向 AstrBot 挂载 Docker socket。

CLI 命令固定为：

```bash
cursor-agent --print --mode ask --model MODEL_ID --output-format json --trust
```

`--trust` 用于本次创建的空目录。插件不传 `--force`、`--yolo` 或会话恢复参数，并生成拒绝文件、Shell、WebFetch 和 MCP 操作的权限配置。容器只挂载专用登录卷、SSH 密钥目录和模型配置，不挂载 AstrBot 数据或其他服务目录。

## 环境要求

| 组件 | 要求 |
| --- | --- |
| AstrBot | 接口依据 4.29.4，声明版本范围为 `>=4.29.4,<5` |
| 部署环境 | Ubuntu、Docker Compose；提供 1Panel 操作说明 |
| CPU 架构 | 当前容器配方为 Linux amd64 |
| Cursor CLI | 固定版本 `2026.11.13-eacade1`，构建时核对 SHA-256 |
| AstrBot 容器 | 需要 OpenSSH 客户端 |

## 安装

完整步骤见 [安装与升级指南](docs/INSTALL.md)。部署分为两部分：

1. 构建专用 Cursor CLI 容器，生成专用 SSH 密钥，并完成 Cursor 登录。
2. 在 AstrBot 安装本插件，配置连接参数，再添加 `cursor_cli` 模型来源。

AstrBot 插件页的仓库地址：

```text
https://github.com/drshenmiao-hash/astrbot_plugin_cursor
```

安装插件不会自动部署 CLI 或登录账号。

## 配置

在插件配置中填写并保存以下参数，然后重载插件。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `model_id` | 空 | 明确的 Cursor 模型 ID，不接受 `auto` 或 `default` |
| `ssh_host` | `cursor-cli` | 专用容器的内部主机名 |
| `ssh_port` | `2222` | 内部 SSH 端口 |
| `identity_file` | `/run/cursor-client/id_ed25519` | 专用客户端私钥的容器内路径 |
| `known_hosts_file` | `/run/cursor-client/known_hosts` | 固定主机公钥的容器内路径 |
| `ssh_binary` | `/usr/bin/ssh` | OpenSSH 客户端路径 |
| `timeout_seconds` | `120` | CLI 超时，范围为 1–600 秒 |

以下三处的模型 ID 必须一致：

- Runner 的 `deploy/model_id` 文件。
- 插件配置的 `model_id`。
- AstrBot 原生模型配置的 `model`。

在 AstrBot 中新增 `cursor_cli`（Cursor CLI）来源，Key 和 API Base 留空，再获取模型列表或手动添加该模型。获取列表不会调用 CLI。选择模型后，关闭对应人格或会话的工具、MCP、图片和音频功能。

## 登录与额度

使用专用容器内的 `cursor-admin login` 完成浏览器登录。插件只接受浏览器登录凭据，不使用 API Key。

模型可用不等于计入套餐额度。首次使用应选择已确认属于套餐范围的明确模型，并在 [Cursor 控制台](https://cursor.com/dashboard) 对照请求前后的用量与收费记录。插件无法决定 Cursor 的账单分类。

测试命令和检查步骤见 [测试与验收指南](docs/ACCEPTANCE.md)。模拟 CLI 不连接 Cursor，不能验证真实登录或计费。

## 使用限制

- 文本上下文上限为 256 KiB，超过后需在 AstrBot 压缩或重置会话。
- 人格以文本 transcript 传入，不是 Cursor 原生的独立 system 消息。
- 采样参数和 token 限额未映射到 CLI。
- Runner 同时处理一个请求，忙时直接返回提示。
- 超时或连接中断的请求仍可能产生用量，重试前应先查看用量记录。

## 参考

- [AstrBot 插件文档](https://docs.astrbot.app/use/plugin.html)与[开发指南](https://docs.astrbot.app/dev/star/plugin-new.html)。
- [astrbot_plugin_openai_oauth](https://github.com/wcqqq1214/astrbot_plugin_openai_oauth)：Provider 注册和热重载设计参考；本项目独立实现，不依赖该插件。
- Cursor CLI 官方文档：[Headless](https://cursor.com/docs/cli/headless)、[参数](https://cursor.com/docs/cli/reference/parameters)、[认证](https://cursor.com/docs/cli/reference/authentication)、[输出格式](https://cursor.com/docs/cli/reference/output-format)、[权限](https://cursor.com/docs/cli/reference/permissions)。

## 许可证

[MIT](LICENSE)
