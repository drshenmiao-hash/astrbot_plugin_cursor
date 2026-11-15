# 测试与验收

测试分为离线模拟、部署检查和真实计费三个部分。测试结果应分别记录；离线模拟不代表真实 Cursor 调用或套餐计费通过。

## 离线模拟 CLI

在已安装 AstrBot 依赖的 Python 环境中，从插件根目录执行：

```bash
python -m unittest discover -s tests -v
```

Docker 示例，容器名按实际测试实例替换：

```bash
docker exec -w /AstrBot/data/plugins/astrbot_plugin_cursor astrbot-test \
  python -m unittest discover -s tests -v
```

`test_provider.py` 和 `test_lifecycle.py` 导入真实 AstrBot API，需要使用 AstrBot 的解释器和依赖。仅运行不依赖 AstrBot 的边界测试：

```bash
python -m unittest tests.test_runner -v
```

测试中的 `mock_cli.py`、`mock_ssh.py` 和凭据均为模拟内容，不连接 Cursor。`mock-plan-model` 是测试专用 ID，不能用于实际模型配置。

| 测试范围 | 检查内容 |
| --- | --- |
| Provider 调用 | 文本请求经模拟 SSH 和 CLI 返回 `LLMResponse` |
| 人格与历史 | 消息顺序正确、原历史不变、不恢复 Cursor 会话 |
| 调用参数 | Ask、明确模型、JSON 输出，无 force 或会话恢复参数 |
| 进程隔离 | 空工作目录、环境变量过滤、权限配置 |
| 错误处理 | 超时、未登录、非零退出、格式错误和输出超限 |
| 输入限制 | 拒绝 Auto、不同模型、非文本内容及过大上下文 |
| 生命周期 | Provider 终止、进程组清理、保留实例的重载和其他实例保留 |

生命周期测试使用 Manager 替身，不覆盖真实 WebUI 或 OpenAI OAuth 插件的完整运行流程。

## 部署检查

在独立测试编排中检查：

- [ ] Runner 能构建和启动，未使用特权模式或 Docker socket。
- [ ] 挂载仅包含专用密钥、模型文件和登录卷，没有 AstrBot 或其他服务目录。
- [ ] CLI 以 UID 10001 运行，工作目录和 HOME 按请求创建，未继承其他服务环境变量。
- [ ] AstrBot 可以添加 `cursor_cli` 来源，模型列表只包含配置的 ID。
- [ ] 冷启动和连续重载不产生重复注册或重复实例，选中的模型保持正确。
- [ ] 停用插件不删除已加载的模型；在模型设置中禁用模型可以清理其请求；原 OpenAI OAuth 插件仍可正常使用。
- [ ] Cursor 仅声明文本能力，普通文本请求不会附带不支持的工具；已配置的备用模型按 AstrBot 原规则接管。
- [ ] QQ 会话的人格及多轮历史正确，不同会话不会复用 CLI 历史。
- [ ] 同时发起请求时，后续请求收到忙提示。
- [ ] 固定版本 CLI 的 Ask 模式和拒绝权限按预期生效。
- [ ] 插件及 Runner 日志中没有聊天正文、登录 URL 或凭据。

## 真实联通与套餐计费

1. 完成 Cursor 浏览器登录，使用 `cursor-admin models` 查看模型 ID，并确认选定模型的套餐范围。
2. 将同一个 ID 写入 Runner、插件和 AstrBot 原生模型配置。
3. 在 [Cursor 控制台](https://cursor.com/dashboard) 记录请求前的时间、套餐用量和按量收费记录。
4. 发送一条简短文本，再发送一条依赖前文的追问，检查回复、人格和历史。
5. 等待用量页面更新，按时间和模型对照记录，确认消耗的是套餐额度，并检查有无额外收费。

登录成功或收到回复都不能单独证明计费归属。页面无法区分时，应记录为“无法确认”。失败或超时的请求也可能产生用量，重试前需核对记录。

| 记录项 | 结果 |
| --- | --- |
| 插件提交、AstrBot 版本、CLI 版本 | |
| Cursor 套餐、明确模型 ID | |
| 模拟测试和部署检查 | |
| 请求时间、请求前后套餐用量 | |
| 请求前后额外收费记录 | |
| 文本回复、人格、多轮历史 | |
| 套餐计费结论 | |
