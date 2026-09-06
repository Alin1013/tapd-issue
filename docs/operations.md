# Dingtalk TAPD Bridge 操作手册

本文按实际操作顺序编写。推荐先用一条测试消息完成只读验证，再开启自动监听；只有将 Node 的 `TAPD_AUTO_CREATE_BUGS` 设为 `false` 时才需要人工确认建单。完整能力和默认业务口径见 [当前内容总览](current-state.md)。

## 0. 先选正确入口

| 场景 | 命令/入口 | 是否要求 @ | 是否直接写 TAPD |
| --- | --- | --- | --- |
| 查找消息，不产生副作用 | `search` | 否 | 否 |
| 只生成草稿 | `draft` | 否 | 否 |
| 人工确认后建单 | `create` | 否 | 仅 `--confirm` |
| 目标群实时内容扫描 | `listen` | 否 | 是，命中主题后自动建 Bug |
| 目标群历史补扫 | `sync` | 否 | 是；`partial` 默认阻断 |
| @缺陷机器人并交给 Node | `agent-listen` | 是，默认 `@缺陷机器人` | 由 Node 按 `TAPD_AUTO_CREATE_BUGS` 决定 |
| 打开 Node 手工表单 | `http(s)://<host>/` | 否 | 提交表单即写入 |

自动流程的默认项目、标题、优先级和责任人规则以 [当前内容总览](current-state.md) 为准；修改前先确认环境变量是否覆盖了默认值。

## 1. 启动前检查

服务器首次安装和更新优先使用 [服务器部署手册](server-deployment.md) 中的两个幂等脚本；本节保留本地/手工启动方式，便于排障。

### Python Bridge

```bash
cd <repo-root>
. .venv/bin/activate
dws auth status
which dws
uvx mcp-server-tapd --help
```

如果还没有虚拟环境：

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

### Node Bug Agent

```bash
cd services/dingtalk-tapd-bug-bot
node --version   # 要求 18+
set -a; . .env; set +a
node --check server.js
node server.js
```

另开终端验证服务：

```bash
curl -fsS http://127.0.0.1:3000/healthz
curl -fsS http://127.0.0.1:3000/api/agent/status
```

`/api/agent/status` 只返回“是否已配置”和非敏感的模型/地址信息，不会返回 token 或密钥。

### 服务器常驻 Python Bridge

Python Bridge 与 Node Bug Agent 是两个独立进程。服务器部署 `listen` 时，建议把仓库安装到
`/opt/dingtalk-tapd`，把状态库和附件放到 `/var/lib/dingtalk-tapd`，并使用仓库提供的
`services/dingtalk-tapd-bridge/dingtalk-tapd-listen.service.example` 创建独立 systemd 单元：

```bash
sudo useradd --system --home /var/lib/dingtalk-tapd --create-home dingtalkbot 2>/dev/null || true
sudo install -d -o dingtalkbot -g dingtalkbot /var/lib/dingtalk-tapd
sudo install -d -o root -g dingtalkbot -m 0750 /opt/dingtalk-tapd
cd /opt/dingtalk-tapd
uv python install 3.11
uv venv --python 3.11 .venv
. .venv/bin/activate
pip install -e .
sudo install -o root -g root -m 0644 \
  services/dingtalk-tapd-bridge/dingtalk-tapd-listen.service.example \
  /etc/systemd/system/dingtalk-tapd-listen.service
```

把 `.env.example` 复制为 `/etc/dingtalk-tapd-listen.env` 后填写 TAPD 凭据和目标群配置：

```bash
sudo install -o root -g dingtalkbot -m 0640 \
  services/dingtalk-tapd-bridge/.env.example \
  /etc/dingtalk-tapd-listen.env
sudo systemctl daemon-reload
sudo systemctl enable --now dingtalk-tapd-listen.service
```

启动前必须以 `dingtalkbot` 用户完成 DWS 登录，并确保该用户能读取登录态：

```bash
sudo -u dingtalkbot -H dws auth status
sudo -u dingtalkbot -H /opt/dingtalk-tapd/.venv/bin/dingtalk-tapd listen --max-events 1
sudo systemctl status dingtalk-tapd-listen.service
sudo journalctl -u dingtalk-tapd-listen.service -f
```

`listen` 会直接创建 TAPD Bug；如果服务器同时运行 `agent-listen` 或钉钉机器人自动建单，
不要让两条链路处理同一目标群，否则当前版本只按消息 ID 去重，无法阻止跨服务的语义重复工单。

服务器自动化安装：

```bash
sudo INSTALL_ROOT=/opt/dingtalk-tapd services/dingtalk-tapd-bridge/install.sh
sudo INSTALL_ROOT=/opt/dingtalk-tapd-bug-bot services/dingtalk-tapd-bug-bot/install.sh
```

脚本只在环境文件不存在时复制模板；已有 `/etc/dingtalk-tapd-listen.env` 和
`/etc/dingtalk-tapd-bug-bot.env` 会保留。填入配置并完成 DWS 登录后，再执行：

```bash
sudo systemctl enable --now dingtalk-tapd-listen.service
sudo systemctl enable --now dingtalk-tapd-bug-bot.service
```

完整的目录、权限、更新备份和自然日历史同步流程见 [服务器部署手册](server-deployment.md)。

## 2. 手工检索与建单

### 第一步：只读搜索

群名必须唯一匹配一个会话；不要在多个候选中自动选第一项。

```bash
dingtalk-tapd search \
  --group "Deepworks问题群" \
  --keyword "登录失败" \
  --start "2026-09-01T00:00:00+08:00" \
  --end "2026-09-05T00:00:00+08:00" \
  --order asc
```

检查输出中的消息数量、`messageId`、`conversationId` 和分页完整性。`complete=false`、`hasMore=true` 或存在 `failures` 时，结果会标记为 `partial`。

### 第二步：生成草稿

`draft` 不调用 TAPD 写接口，只生成待确认内容：

```bash
dingtalk-tapd draft \
  --group "Deepworks问题群" \
  --keyword "登录失败" \
  --workspace-id 57379524 \
  --type bug \
  --owner "雷艾琳" \
  --priority "高"
```

`--type` 支持 `bug`、`stories`、`tasks`。未提供 `--workspace-id` 时，可用 `--user-name` 查询参与项目；只有恰好一个项目时才会继续。

### 第三步：预览并确认

第一次执行 `create` 不加 `--confirm`，只输出项目元数据、字段定义、搜索结果和草稿：

```bash
dingtalk-tapd create \
  --group "Deepworks问题群" \
  --keyword "登录失败" \
  --workspace-id 57379524 \
  --type bug
```

确认 workspace、类型、标题、负责人和字段后，再执行：

```bash
dingtalk-tapd create \
  --group "Deepworks问题群" \
  --keyword "登录失败" \
  --workspace-id 57379524 \
  --type bug \
  --priority "高" \
  --fields-json '{"自定义字段":"候选值"}' \
  --confirm
```

自定义字段必须使用预览中 TAPD 返回的真实字段名或别名。检索结果为 `partial` 时，必须缩小范围，或显式增加 `--allow-partial` 并承担漏数风险。

## 3. 自动建单

### 实时监听

先用一条事件验证过滤和写入链路：

```bash
dingtalk-tapd listen --max-events 1
```

确认 JSON 结果出现预期的 `created`（相关消息）后再常驻；`ignored` 表示非目标主题，`duplicate` 表示事件已处理，`failed` 需要查看错误并人工排查：

```bash
dingtalk-tapd listen
# 或限定本次运行时长
dingtalk-tapd listen --duration 10m
```

`--max-events` 统计监听器接收的去重事件，不等于成功创建的 Bug 数量。临时验证结束后可按 `Ctrl-C` 停止进程；DWS 子订阅会先尝试正常关闭。

实时监听订阅每个目标群的全部消息，不要求消息带 @；通过后，程序会：

1. 用 `conversationId:messageId` 去重。
2. 下载消息详情和附件，必要时尝试 OCR。
3. 只对包含“企业知识中心/知识库/知识管理”等主题的消息建 Bug。
4. 按影响词映射优先级，并写入来源消息 ID。
5. 先读取 workspace/字段做写前校验，再创建 Bug，最后尝试 `get_bug` 核对。

### 历史同步

历史同步不要求消息带 @，但仍要求正文或 OCR 内容命中企业知识中心主题：

```bash
dingtalk-tapd sync \
  --start "2026-09-01T00:00:00+08:00" \
  --end "2026-09-05T00:00:00+08:00" \
  --order desc
```

结果为 `partial` 时默认只报告完整性告警，不会自动写入。确认范围后才使用：

```bash
dingtalk-tapd sync \
  --start "2026-09-01T00:00:00+08:00" \
  --end "2026-09-05T00:00:00+08:00" \
  --allow-partial
```

写入前校验失败修复后，可仅补偿已知的前置失败：

```bash
dingtalk-tapd sync \
  --start "2026-09-01T00:00:00+08:00" \
  --end "2026-09-05T00:00:00+08:00" \
  --retry-failed
```

未知写入结果不会自动重试，以免重复创建 TAPD Bug。查看 `.dingtalk-tapd/state.sqlite3` 对应结果后再人工处理。

## 4. 远端 Bug Agent

适合 TAPD/GPT 服务与 DWS 监听器不在同一台机器的场景。

### 监听器端

```bash
export DINGTALK_TAPD_AGENT_EVENTS_URL="https://bug.example.com/api/agent/events"
export DINGTALK_TAPD_AGENT_SECRET="与服务端相同的随机密钥"
dingtalk-tapd agent-listen --max-events 1
```

验证成功后再常驻：

```bash
dingtalk-tapd agent-listen
```

监听器负责 DWS 事件过滤、附件下载和 HMAC 签名；它不会携带 TAPD 凭据，也不会直接创建 TAPD Bug。服务端收到事件后立即返回 `202 accepted`，后台完成模型分析、责任人匹配和后续建单；重复的 `eventId` 返回 `duplicate`。

### 服务端端到端流程

1. 钉钉机器人收到文本、截图或视频。
2. Node 服务校验回调签名并下载媒体。
3. TAPD 动态字段加载后，模型生成 JSON 草稿。
4. 按 `TAPD_RESPONSIBILITY_WHITELIST` 解析负责人、开发人和测试人，并校验项目成员账号。
5. 默认 `TAPD_AUTO_CREATE_BUGS=true`：直接调用 TAPD `/bugs`，再逐个上传附件。
6. `TAPD_AUTO_CREATE_BUGS=false`：投递互动卡片；卡片未配置或投递失败时，回退到 `/draft/{id}` 草稿链接，等待人工确认。
7. 自动建单成功后，通过临时 `sessionWebhook` 或机器人群消息 API 回传统一成功提醒；有 `senderStaffId` 时会 @ 提问人。建单成功但通知失败只记日志，不会因重试通知而重复创建 Bug。

自动模式不会等待卡片确认；人工模式的草稿默认 30 分钟过期，服务重启会清空尚未确认的草稿。Node 的自动建单也会先读取 TAPD 动态字段并校验责任人，附件上传失败不会回滚已经创建的 Bug。

## 5. Node H5 与互动卡片

### H5 表单

访问 `http://localhost:3000/` 可直接打开手工建单页面。表单字段来源：

- 模块、发现版本：TAPD `bugs/get_fields_info`。
- 迭代：TAPD `/iterations`。
- 发布计划：TAPD `/releases`。
- 处理人：TAPD `/workspaces/users`。

媒体支持最多 5 个文件，单个最多 8 MB，总计最多 12 MB。创建 Bug 成功但附件上传失败时，页面会同时显示已上传和失败列表。

### 互动卡片

卡片模板发布后，在服务器运行：

```bash
sudo /opt/dingtalk-tapd-bug-bot/configure-interactive-card.sh
```

脚本会写入模板 ID、RouteKey、Secret、公网地址，注册 `${PUBLIC_BASE_URL}/dingtalk/card-callback`，重启服务并检查状态。只有 `TAPD_AUTO_CREATE_BUGS=false` 时卡片确认才是建单必经步骤；卡片统一私投给 `DINGTALK_CARD_REVIEW_RECIPIENT_ID`，默认不是提问人。

## 6. systemd 运维

安装 `services/dingtalk-tapd-bug-bot/dingtalk-tapd-bug-bot.service.example` 前，确认已创建 `dingtalkbot` 用户、工作目录和媒体目录，并按服务器实际路径调整 `WorkingDirectory`、`ExecStart` 和 `ReadWritePaths`：

```bash
sudo install -m 0644 dingtalk-tapd-bug-bot.service.example \
  /etc/systemd/system/dingtalk-tapd-bug-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now dingtalk-tapd-bug-bot.service
```

常用命令：

```bash
sudo systemctl status dingtalk-tapd-bug-bot.service
sudo systemctl restart dingtalk-tapd-bug-bot.service
sudo journalctl -u dingtalk-tapd-bug-bot.service -f
curl -fsS http://127.0.0.1:3000/healthz
curl -fsS http://127.0.0.1:3000/api/agent/status
```

更新配置后必须重启服务。修改 TAPD OAuth 可以使用：

```bash
sudo /opt/dingtalk-tapd-bug-bot/configure-tapd-credentials.sh
```

更新 OpenAI、钉钉 AppKey 和 RobotCode 可以使用：

```bash
sudo /opt/dingtalk-tapd-bug-bot/configure-bug-agent.sh
```

脚本会把密钥写入 root-only 环境文件；输入密钥时不会回显。

## 7. 故障排查

| 现象 | 先检查 | 处理 |
| --- | --- | --- |
| `dws` 找不到或监听不 ready | `which dws`、`dws auth status`、stderr 日志 | 安装/登录 DWS；必要时设置 `DWS_EXECUTABLE`、`DWS_PROFILE` |
| 群名零命中或多命中 | `search` 的群解析结果 | 使用唯一群名或直接配置稳定 `DINGTALK_TAPD_GROUP_ID` |
| 输出为 `partial` | `integrity`、`hasMore`、`failures` | 缩小时间范围；确认完整后再加 `--allow-partial` |
| TAPD 401/403 | `/api/agent/status`、令牌/项目权限 | 优先配置 OAuth；确认 workspace、字段读取、成员和附件权限 |
| Bug Agent 提示未配置模型 | `openai.configured`、`OPENAI_BASE_URL`、`OPENAI_MODEL` | 设置 API Key 和实际可用模型；中转站按能力设置 API mode |
| 图片/视频无法下载 | `dingTalkMedia` 状态、`DINGTALK_ROBOT_CODE` | 配置 RobotCode，并提供静态 access token 或 AppKey/Secret |
| 互动卡片没出现 | `automation.autoCreateBugs`、`interactiveCard` 状态、服务日志 | 自动模式下不需要卡片；人工模式下三项卡片配置必须齐全，失败时使用草稿 H5 链接 |
| 钉钉回调 401 | `DINGTALK_CLIENT_SECRET`、公网 HTTPS | 检查回调 Secret、时间戳和反向代理是否保留请求头 |
| 草稿链接失效 | 草稿是否超过 30 分钟或服务是否重启 | 重新发送截图/视频生成草稿 |
| Bug 已创建但附件失败 | 返回的 `attachmentFailures`、服务日志 | 检查 `MEDIA_DIR`、TAPD 附件权限和文件大小；可在 TAPD 手动补传 |
| 桥接返回 `duplicate` | `eventId` 是否重复 | 这是 30 分钟内的幂等保护；确认原事件的服务日志和草稿状态 |

## 8. 日常变更规则

- 修改目标群、workspace 或负责人后，先用 `search`/`draft` 做只读验证，再开启 `listen`/`sync`。
- 修改模型提示词、字段映射或责任人白名单后，先用 `MOCK_TAPD=true` 验证完整流程，再用单条测试消息验证真实 TAPD。
- 修改回调、卡片或签名逻辑后，同时检查 `/healthz`、`/api/agent/status` 和一条实际钉钉回调。
- 新增外部写接口时保留自动模式的责任白名单、来源 ID、幂等键和错误可见性；人工回退模式继续保留确认门禁，不要把未知写入结果自动重试。
- 生产发布前确认环境文件权限、HTTPS、日志脱敏、媒体目录清理和 systemd 自动重启状态。

## 9. 一次完整验收

每次首次部署或修改关键配置后，按以下顺序验收，避免直接用真实历史批量写入：

1. `dws auth status`、`which dws`、`uvx mcp-server-tapd --help`，确认本地依赖和登录状态。
2. `dingtalk-tapd search` 验证群名唯一、时间范围和 `integrity`；必要时再执行 `draft`。
3. Node 执行 `node --check server.js`，启动后检查 `/healthz` 和 `/api/agent/status`。
4. 本地流程用 `listen --max-events 1`；桥接流程用 `agent-listen --max-events 1`，确认 @ 目标、媒体和返回状态。
5. Node 本地流程先用 `MOCK_TAPD=true`；真实 TAPD 只发送一条明确测试消息，核对责任人、标题、来源和附件。
6. 确认成功通知可回到会话/目标群，并检查 `journalctl` 中没有凭据、完整媒体 data URI 或内部路径泄漏。
