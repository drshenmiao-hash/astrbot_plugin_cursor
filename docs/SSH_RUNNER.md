# SSH 专用 Runner

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

配置 `transport=ssh`、`ssh_host=cursor-cli`、`ssh_port=2222`，使用默认专用私钥及 known_hosts 路径。修改主机名/端口时同步 known_hosts，保留主机校验。在插件填入首次校验与注册的模型 ID。聊天使用 AstrBot 当前选择的已启用 Cursor 模型。

SSH 清理回执要求插件和运行器的协议版本匹配。可先用 `/cursor_doctor` 检查 `runner_cleanup_protocol`。旧运行器会报 `runner_cleanup_unsupported`。独立校验结束后，只有运行器确认本次 ACP 子进程树已退出并释放锁，插件才发布校验结果。传输中断或回执超时会报 `cleanup_unverified`，可安全检查远端状态后再重载。

SSH 镜像内置健康检查；未登录时显示 unhealthy 是预期结果。诊断分别返回客户端密钥、主机校验、SSH 版本、登录卷和 CLI 构建入口哈希。入口哈希校验不是对整个 Docker 镜像 digest 的远程证明。
