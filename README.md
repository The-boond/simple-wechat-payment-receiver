# 简陋的微信收款接收器

一个可自行配置的 Windows / Linux 微信桌面收款消息采集器。它监听微信业务消息数据库的 WAL 变化，仅在新变化发生后截图并 OCR“微信收款助手”中的最新收款卡片，然后把标准化事件通过带时间戳的 HMAC 请求发送到你的业务接口。

> 这不是微信支付官方商户 API。软件采集模式需要一台持续登录微信桌面客户端的 Windows 或 Linux 设备。生产订单的匹配、完成、退款和金额校验仍应由你的服务端实现。

## 功能

- Windows：业务消息 WAL 触发 + Windows OCR。
- Linux：业务消息 WAL 触发 + X11 截图 + Tesseract OCR。
- 启动只建立基线，不重放历史收款记录。
- 明确金额、月日和时分必须接近 WAL 触发时间。
- 多次 OCR 尝试与可配置窗口位置，适应不同微信界面布局。
- HMAC-SHA256 签名、事件 ID、持久 spool、指数退避和幂等重试。
- 默认不保存完整 OCR 文本、截图或所有系统通知。
- Windows 通知读取、窗口恢复均为显式可选配置。
- 附带一个最小 HMAC Webhook 接收示例。

## 已移除的部署专用内容

公开版本不包含：

- 真实域名、Token、订单号、用户 ID、微信账号目录和支付记录。
- 生产数据库、XBoard 或其他站点的直接改单逻辑。
- 一次性补单、硬编码金额/时间和远程服务器运维命令。
- 默认读取所有 Windows 通知或默认操控隐藏窗口的行为。
- 真实截图、OCR 日志、spool 和登录数据。

## 目录

```text
receiver_core.py                 公共事件、解析、签名、spool、重试
windows_agent.py                 Windows Agent
linux_agent.py                   Linux Agent
configs/windows.example.json     Windows 完整配置示例
configs/linux.example.json       Linux 完整配置示例
scripts/windows/wechat_ocr.ps1   Windows allowlist OCR
examples/webhook_receiver.py     最小签名接收示例
deploy/linux/                    Linux systemd 示例
tests/                           自动化测试
```

## 事件接口

Agent 向 `bridge.url` POST JSON：

```json
{
  "event_id": "evt_...",
  "provider": "wxpay",
  "channel_id": "7821",
  "amount": "18.88",
  "occurred_at": 1784644380,
  "external_txn_id": null,
  "trade_no": null,
  "payer": null,
  "raw_text": "微信收款到账 ￥18.88元 时间 07-21 22:33:00",
  "source": "wechat-linux-wal-ocr",
  "agent_id": "linux-collector-01"
}
```

请求头：

```text
X-Bridge-Token: SHARED_SECRET
X-Bridge-Event-Id: evt_...
X-Bridge-Timestamp: UNIX_SECONDS
X-Bridge-Signature: HMAC_SHA256(secret, timestamp + "." + raw_json_body)
```

接收端成功时返回：

```json
{"ok": true, "result": "accepted", "event_id": "evt_..."}
```

重复事件可返回 `ok=true, result=already_processed`。临时失败返回 `ok=false`，Agent 会保留 pending 并重试。

## 公共配置

复制对应示例：

```bash
cp configs/linux.example.json config.json
# Windows PowerShell:
# Copy-Item configs\windows.example.json config.json
```

必须修改：

| 配置 | 说明 |
|---|---|
| `bridge.url` | 你的 HTTPS 事件接口 |
| `bridge.token_env` | 保存共享密钥的环境变量名 |
| `agent.id` | 每台采集器的唯一 ID |
| `channel.id` | 你的业务通道 ID |
| `*.trigger_files` | 当前微信账号的 `biz_message_0.db-wal` 路径或 glob |
| Windows `window_title_regex` | 当前微信窗口标题表达式 |
| Linux `display` | 微信所在 X11 DISPLAY |
| Linux `window_name_regex` | 收款助手独立窗口标题表达式 |

常用可调项：

- `parser.timezone`
- `parser.receipt_pattern`
- `parser.max_event_age_seconds`
- `runtime.poll_seconds`
- `runtime.spool_dir`
- `trigger_quiet_seconds`：WAL 最后一次变化后的静默等待，避免文件仍在写入时过早 OCR
- `capture_attempts` 的延迟和滚动位置
- OCR 命令、语言、PSM、截图保留策略
- Windows 进程名、通知 allowlist、窗口恢复开关

## 共享密钥

生成密钥：

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Linux：

```bash
export WECHAT_RECEIVER_TOKEN='你的随机密钥'
```

Windows PowerShell（当前会话）：

```powershell
$env:WECHAT_RECEIVER_TOKEN = '你的随机密钥'
```

配置文件内明文 Token 默认不会被采用。若本地测试确实需要，可显式设置 `bridge.allow_inline_secret=true` 和 `bridge.token`，并确保文件没有提交。

## Windows

要求：

- Windows 10/11
- Python 3.10+
- 微信 4.x
- Windows 中文 OCR 语言包

1. 打开“微信收款助手”，确认最新收款卡片可见。
2. 找到当前账号的 `biz_message_0.db-wal`，填写 `windows.trigger_files`。
3. 默认保持 `include_notifications=false`、`allow_window_restore=false`。
4. 先检查配置：

```powershell
py -3 .\windows_agent.py --config .\config.json --once
```

5. 启动：

```powershell
.\scripts\windows\run-agent.ps1 -Config .\config.json
```

托盘状态下需要自动恢复窗口时，再启用：

```json
"allow_window_restore": true
```

该选项只处理配置进程和标题 allowlist 匹配的窗口，并在 OCR 后恢复原来的最小化/隐藏状态。

## Linux

Ubuntu/Debian 常用依赖：

```bash
sudo apt-get install -y python3 tesseract-ocr tesseract-ocr-chi-sim \
  imagemagick xdotool wmctrl inotify-tools fonts-noto-cjk
```

要求微信运行在持久 X11 会话（真实桌面或 Xvfb），并提前打开独立的“微信收款助手”窗口。

检查配置：

```bash
python3 linux_agent.py --config config.json --once
```

启动：

```bash
python3 linux_agent.py --config config.json
```

`deploy/linux/` 提供 systemd 和环境文件示例。替换 `YOUR_USER`、路径和 DISPLAY 后安装；环境文件权限建议为 `0600`。

## 本地接收测试

终端 1：

```bash
export WECHAT_RECEIVER_TOKEN='test-secret'
python3 examples/webhook_receiver.py
```

配置 Agent：

```json
"bridge": {
  "url": "http://127.0.0.1:8787/event",
  "token_env": "WECHAT_RECEIVER_TOKEN",
  "allow_http_localhost": true
}
```

健康检查：`http://127.0.0.1:8787/health`。事件保存在示例进程目录的 `events.sqlite3`。

## 服务端匹配建议

接收端至少应实现：

1. 验证 HMAC 与时间戳。
2. 以 `event_id` 建立唯一索引。
3. 校验 provider、channel、金额范围和事件时间。
4. 只匹配时间窗口内唯一的待支付订单。
5. 同金额存在多笔订单时保留待确认状态。
6. 完成订单、发货或开通服务必须使用数据库事务和幂等保护。

## 测试

```bash
python -m unittest discover -s tests -v
python -m py_compile receiver_core.py windows_agent.py linux_agent.py examples/webhook_receiver.py
```

## 常见问题

### 只有 heartbeat

检查 WAL 路径是否存在、微信是否已登录、收款助手窗口是否打开。

### 有 trigger，没有 candidate

调整 `capture_attempts`、窗口标题、OCR 语言或 `receipt_pattern`。Linux 可按 `0/2/4` 的滚动位置重试。

### pending 持续增加

检查接收端 HTTPS、Token、HMAC 算法、响应 JSON 和订单匹配逻辑。

### 是否需要一直开自己的电脑

采集设备需要保持微信登录；可放在持续运行的 Windows 主机或 Linux 云服务器。SSH/VNC 断开后，systemd 服务仍可运行。

## License

[MIT](LICENSE)
