# Dingtalk TAPD Bridge

这是一个围绕设计文档实现的 DWS→TAPD 桥接项目。Python CLI 负责群消息读取、主题筛选、来源追踪和自动建单；Node Bug Agent 负责钉钉机器人回调、媒体分析、责任人校验和 TAPD 写入。`search`、`draft`、`create` 保持读取与写入分层；`listen`、`sync` 和 `agent-listen` 提供实时、历史和远端桥接入口。

## 文档导航

- [配置说明](docs/configuration.md)：环境变量、权限、数据生命周期和二次开发入口。
- [操作手册](docs/operations.md)：安装验证、手工建单、自动监听、远端 Agent、systemd 运维和故障排查。
- [服务器部署手册](docs/server-deployment.md)：两套 systemd 服务、环境文件、更新发布和历史同步验收。
- [当前内容总览](docs/current-state.md)：当前能力、默认业务口径、状态语义和已知边界。

## 安装

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

运行时需要本机已登录的 `dws` CLI。TAPD 默认通过官方 `mcp-server-tapd`（命令为 `uvx mcp-server-tapd`）调用，令牌由 MCP Server 从以下环境变量读取：

```bash
export TAPD_ACCESS_TOKEN=...
# 或者
export TAPD_API_USER=...
export TAPD_API_PASSWORD=...
```

可选环境变量：`DWS_EXECUTABLE`、`DWS_PROFILE`、`TAPD_BACKEND`（默认 `mcp`，可设为 `rest`）、`TAPD_MCP_COMMAND`、`TAPD_API_BASE_URL`、`TAPD_BASE_URL`。

## 自动建单

已完成 DWS 设备登录后，可以启动常驻监听：

```bash
dingtalk-tapd listen
```

监听会为配置中的每个群分别建立 `dws event consume user_im_message_receive_group --group <openConversationId> --flatten --format ndjson` 订阅，默认同时兼容 `DeepWorks 产品交流群` 和“测试机器人”。`listen` 会读取目标群全部消息，再按正文/OCR 内容筛选企业知识中心、知识库或知识管理主题；只接收 @缺陷机器人的桥接场景请使用 `agent-listen`。没有 @ 的历史消息请使用下面的 `sync` 命令扫描。

- 使用 TAPD 项目 `57379524`，自动工单类型固定为 Bug；未命中模块责任规则时负责人回退为 `雷艾琳`；解析出功能模块后按默认分工设置处理人和开发人：编译→杨耀发、抽取→肖文杨、本体→肖文杨、对话部分→杨耀发；
- 根据影响词设置优先级（明确紧急/P0 为 `urgent`，阻断故障为 `high`，建议/咨询为 `low`，无法判断为 `medium`）；
- 生成 `【企业知识中心—用户反馈】问题描述` 标题；
- 用 `+messages-mget --download-resources` 下载截图/附件，在描述中保留本地路径、资源 ID、消息 ID 和下载失败原因；
- 对本地图片尝试使用系统 `tesseract` 做 OCR（默认 `eng`，可通过 `DINGTALK_TAPD_OCR_COMMAND` 自定义）。OCR 或下载失败时会明确标注原图待查看，不会虚构截图内容；
- 以 `conversationId:messageId` 写入 `.dingtalk-tapd/state.sqlite3`，同一消息只建一次工单。

可用边界参数控制监听生命周期：

```bash
dingtalk-tapd listen --duration 10m
dingtalk-tapd listen --max-events 1
```

需要补扫历史聊天记录时执行一次同步。可以用时间范围限制同步窗口；省略时沿用 DWS 最近消息默认范围：

```bash
dingtalk-tapd sync \
  --start "2026-09-01T00:00:00+08:00" \
  --end "2026-09-05T00:00:00+08:00"
```

如果此前因为 TAPD workspace 或字段只读校验失败，可在凭据修复后显式补偿：

```bash
dingtalk-tapd sync \
  --start "2026-09-04T00:00:00+08:00" \
  --end "2026-09-06T00:00:00+08:00" \
  --retry-failed
```

`--retry-failed` 只允许重试已知的写入前校验错误；`create_bug` 返回未知结果的事件仍保持幂等保护，不会自动重试。

同步结果为 partial 时默认只输出完整性告警、不自动写入；确认时间范围后可显式加 `--allow-partial`。

高级覆盖项（正常使用不需要填写）：`DINGTALK_TAPD_GROUPS`、`DINGTALK_TAPD_GROUP_ID`、`DINGTALK_TAPD_GROUP_NAME`、`DINGTALK_TAPD_WORKSPACE_ID`、`DINGTALK_TAPD_OWNER`、`DINGTALK_TAPD_DEVELOPER`、`DINGTALK_TAPD_TESTER`（默认 `雷艾琳`）、`DINGTALK_TAPD_RESPONSIBILITY_WHITELIST`、`DINGTALK_TAPD_TITLE_PREFIX`、`DINGTALK_TAPD_STATE_DB`、`DINGTALK_TAPD_ATTACHMENT_DIR`、`DINGTALK_TAPD_READY_TIMEOUT`、`DINGTALK_TAPD_OCR_COMMAND`、`DINGTALK_TAPD_MENTION_TARGETS`、`DINGTALK_TAPD_MENTION_TARGET_IDS`、`DINGTALK_TAPD_BOT_MENTION_TARGETS`（默认 `缺陷机器人`）、`DINGTALK_TAPD_BOT_MENTION_TARGET_IDS`（默认使用缺陷机器人的稳定 ID）。`DINGTALK_TAPD_GROUPS` 使用 `[ {"id":"openConversationId","name":"群名"} ]`；旧的单群 ID/名称变量仍可继续使用，显式设置它们时只监听该群。白名单使用 JSON 数组或对象，规则中的 `match/module/keywords` 用于匹配消息，`owner/current_owner` 和 `developer/de` 用于分配字段。附件目录必须是工作目录内相对路径，符合 DWS 的下载安全约束。

TAPD MCP 只有在工具契约声明支持时才接受图片/视频公网直链作为富媒体；旧版 MCP 会自动降级为把直链写入描述。钉钉下载到本地的文件不会被冒充成“已上传”，当前未实现把本地文件直接上传到 TAPD 的未验证接口。

该监听默认以当前用户 OAuth 身份运行，不需要创建机器人。若改为企业机器人 Stream，需要额外的开放平台应用、发布审批和入群配置，不能由本项目自动猜测或代办。

只有在明确设置 `TAPD_BACKEND=rest` 时才使用内置 REST 兼容客户端；生产环境建议使用 MCP 后端，以复用设计文档中列出的官方工具契约。

### 连接远端 Bug Agent

如果 TAPD/GPT/互动卡片服务运行在独立服务器（例如 `10.201.0.151` 的
`dingtalk-tapd-bug-bot.service`），可以让本地 DWS 监听器把目标群事件交给该服务：

```bash
export DINGTALK_TAPD_AGENT_EVENTS_URL="https://<公网域名>/api/agent/events"
export DINGTALK_TAPD_AGENT_SECRET="与远端 AGENT_INGEST_SECRET 相同的随机字符串"
dingtalk-tapd agent-listen --max-events 1
```

`agent-listen` 只负责监听、下载并签名转发消息；远端服务继续执行配置的模型（默认 `gpt-5.6-sol`）分析、责任人
白名单匹配和 TAPD Bug 创建。媒体以受限 data URI 转发，不会把本地路径或 TAPD
凭据发送到监听器以外的地方。远端返回 `duplicate` 时表示同一
`conversationId:messageId` 已经处理，不会重复提单。

远端服务需配置 `AGENT_INGEST_SECRET`，并把钉钉机器人加入目标群、配置 `/dingtalk/callback`
回调；默认不需要卡片确认，成功结果会通过临时 `sessionWebhook` 或机器人群消息 API 回传，并在有稳定
`senderStaffId` 时 @ 提问人。设置 `TAPD_AUTO_CREATE_BUGS=false` 时，卡片确认仍由
`/dingtalk/card-callback` 处理。`agent-listen` 只订阅目标群并放行 `@缺陷机器人`，互动卡片统一私投给
`DINGTALK_CARD_REVIEW_RECIPIENT_ID`（默认是雷艾琳），不会私投给提问人。

监听器的高级配置：`DINGTALK_TAPD_AGENT_TIMEOUT`（默认 30 秒）。公网地址和共享密钥只放
在 systemd/环境变量中，不要写进 Git 或 README 实例值。

服务器 `10.201.0.151:/opt/dingtalk-tapd-bug-bot` 的可部署源代码已同步到
`services/dingtalk-tapd-bug-bot/`，并补上了上述 `/api/agent/events` 桥接端点。同步内容包含
当前 `server.js`、Node 测试、配置脚本、环境变量模板和互动卡片模板；真实 `.env`、部署备份、
`server.js.bak-*` 以及 macOS `._*` 元数据被刻意排除，避免凭据和历史运行产物进入 Git。

## 使用

只读检索：

```bash
dingtalk-tapd search \
  --group "Deepworks问题群" \
  --keyword "登录失败" \
  --start "2026-09-01T00:00:00+08:00" \
  --end "2026-09-05T00:00:00+08:00"
```

生成草稿（不会访问 TAPD 写接口）：

```bash
dingtalk-tapd draft \
  --group "Deepworks问题群" --keyword "登录失败" \
  --workspace-id 123456 --type bug
```

创建工单分两步。第一次不带 `--confirm` 会输出项目元数据、自定义字段和完整草稿；确认内容无误后再次带上 `--confirm`。若搜索结果是 partial，还必须显式加 `--allow-partial`。

```bash
dingtalk-tapd create \
  --group "Deepworks问题群" --keyword "登录失败" \
  --workspace-id 123456 --type bug --confirm
```

`--type` 支持 `bug`、`stories`、`tasks`。自定义字段使用 JSON 对象传入，例如 `--fields-json '{"priority":"高"}'`；生产环境应先查看命令输出的 TAPD 字段定义再填写。

不确定项目 ID 时，可以改用 `--user-name`。只有 TAPD 返回恰好一个参与项目时才会继续；多个项目会要求显式提供 `--workspace-id`。

## 安全边界

- 群名必须唯一解析，零命中或多候选不会自动选择。
- DWS 分页缺少 `complete=true`、存在 `hasMore` 或 `failures` 时结果会标记为 partial。
- 手动 `create` 仍需要 `--confirm`；`listen` 和 `sync` 在目标群消息被分析为企业知识中心相关内容后自动写入，并把 `te` 测试人统一设置为 `雷艾琳`。
- 令牌只从环境变量读取，不写入 JSON 输出、日志或仓库。
- `listen` 和 `sync` 的事件状态、去重键持久化在 `.dingtalk-tapd/state.sqlite3`；写入结果未知时标记为失败供人工排查，不自动重试，只有显式 `--retry-failed` 才会补偿已知的写入前校验失败。
