# 钉钉-TAPD Bug Agent

这是一个不依赖第三方 npm 包的 Node.js 服务，负责钉钉输入、媒体分析和 TAPD Bug 写入。它既支持钉钉机器人原生回调，也支持仓库根目录 Python Bridge 的签名事件桥接。

默认链路是：

```text
钉钉文本/截图/视频 -> Bug Agent 分析 -> 自动选择责任人 -> TAPD POST /bugs -> 钉钉回执
```

默认 `TAPD_AUTO_CREATE_BUGS=true`，模型分析后直接创建 Bug；设置为 `false` 才进入可编辑草稿、互动卡片或 H5 人工确认流程。仓库级能力总览见 [`docs/current-state.md`](../../docs/current-state.md)。

## 1. 本地启动

环境要求：Node.js 18 或更高版本。

```bash
cp .env.example .env
npm test
MOCK_TAPD=true node server.js
```

打开 <http://localhost:3000>，可以直接填写表单验证页面流程。`MOCK_TAPD=true` 时不会访问 TAPD，只返回一个模拟 Bug ID。

项目没有引入 dotenv。启动时可以使用 shell 导入配置，或者直接设置环境变量：

```bash
export TAPD_WORKSPACE_ID=你的项目ID
export TAPD_ACCESS_TOKEN=你的TAPD_OAuth_access_token
export TAPD_CLIENT_ID=你的TAPD应用ID
export TAPD_CLIENT_SECRET=你的TAPD应用密钥
export DINGTALK_CLIENT_SECRET=钉钉企业机器人ClientSecret
export DINGTALK_APP_KEY=钉钉企业应用AppKey
export DINGTALK_FORM_SECRET=随机生成的长密钥
export PUBLIC_BASE_URL=https://你的公网HTTPS地址
export OPENAI_API_KEY=你的OpenAI_API_KEY
export OPENAI_BASE_URL=https://dts.deepexi.com/v1
export OPENAI_MODEL=gpt-5.6
export OPENAI_API_MODE=responses
node server.js
```

服务器部署可将 `dingtalk-tapd-bug-bot.service.example` 安装为 systemd unit，再按实际安装
目录调整 `WorkingDirectory`、`ExecStart` 和 `ReadWritePaths`。真实环境文件仍应放在
`/etc/dingtalk-tapd-bug-bot.env`，不要复制到仓库。

## 2. 钉钉配置

在钉钉企业内部机器人中配置 HTTP 消息接收地址：

```text
https://你的公网HTTPS地址/dingtalk/callback
```

代码会：

- 校验 `timestamp`、`sign`（配置 `DINGTALK_CLIENT_SECRET` 后启用）。
- 识别 `新建Bug`、`创建Bug`、`建Bug` 等文本。
- 使用机器人回调中的 `sessionWebhook` 回复 ActionCard。
- ActionCard 打开同一服务的 `/` 表单页。
- Bug Agent 模式下，在已加入机器人的群里 @缺陷机器人并发送文字、图片或视频即可开始分析；默认会直接创建 Bug，并通过临时 `sessionWebhook` 或机器人群消息 API 回传统一成功提醒，有稳定 `senderStaffId` 时会 @ 提问人。当前“测试机器人”群已验证该回执链路。

钉钉必须能够从公网访问回调地址。开发阶段可以使用带 HTTPS 的公网隧道；生产环境建议部署到云函数、容器服务或一台有固定公网 HTTPS 域名的服务器。

## 3. TAPD 配置

推荐配置 TAPD OAuth。长期运行建议配置应用 ID/密钥，让服务自动刷新短期 Token：

```bash
export TAPD_API_BASE_URL=https://api.tapd.cn
export TAPD_WORKSPACE_ID=目标项目ID
export TAPD_CLIENT_ID=你的TAPD应用ID
export TAPD_CLIENT_SECRET=你的TAPD应用密钥
```

也可以临时只配置 `TAPD_ACCESS_TOKEN`；Token 过期后动态字段和建单会停止，需手工替换。

服务调用：

```text
POST https://api.tapd.cn/bugs
Authorization: Bearer ACCESS_TOKEN
```

如果只是验证接口，也可以临时使用 `TAPD_API_USER` 和 `TAPD_API_PASSWORD` 走 Basic Auth 兼容方式，但不要把账号密码放进前端或钉钉链接。

## 4. 关键接口

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/healthz` | 健康检查 |
| GET | `/api/agent/status` | 查看 Bug Agent 是否配置完成（不返回密钥） |
| POST | `/dingtalk/card-callback` | 互动卡片按钮回调，确认后创建 Bug |
| GET | `/` | H5 建单表单 |
| GET | `/api/options?workspace_id={id}` | 从 TAPD 加载模块、发现版本、迭代候选值 |
| GET | `/media/{id}` | 查看已上传的截图或视频 |
| GET | `/draft/{id}` | 查看并修改 AI Bug 草稿 |
| POST | `/dingtalk/callback` | 钉钉机器人回调 |
| POST | `/api/agent/events` | 接收本地 DWS 监听器的 HMAC 签名事件 |
| POST | `/api/bugs` | 校验表单并创建 TAPD Bug |
| POST | `/api/drafts/{id}/confirm` | 兼容旧草稿模式，手工确认后创建 TAPD Bug |

### 本地 DWS 监听器桥接

仓库根目录的 Python CLI 可以把目标群事件转发到本服务的 `/api/agent/events`。服务端
要求 `AGENT_INGEST_SECRET`，请求头为 `x-agent-timestamp` 和
`x-agent-signature`，签名内容是 `timestamp + "\\n" + 原始 JSON 请求体` 的 HMAC-SHA256
十六进制值；时间戳超过 5 分钟的请求会被拒绝。事件使用 `eventId` 去重 30 分钟，媒体
继续受单个 8MB、总计 12MB 和最多 5 个文件的限制。

接口收到合法事件后立即返回 `202 accepted`，后台完成 TAPD 字段读取、模型分析、责任人匹配
和建单。事件 `eventId` 在 30 分钟内重复时返回 `duplicate`。默认不会等待人工确认；设置
`TAPD_AUTO_CREATE_BUGS=false` 才会保留可访问的草稿链接并等待卡片或 H5 确认。

## 5. 当前边界与上线前检查

当前代码定位为快速 PoC，正式使用前至少需要：

- `pendingSessions`、`bugDrafts` 和桥接事件去重目前都在内存；服务重启会丢失未确认草稿和短期去重状态，跨重启可靠运行前应迁移到 Redis 或数据库。
- 增加钉钉 `senderStaffId` 到 TAPD 用户的映射和项目权限校验。
- 从 TAPD `/bugs/get_fields_info` 动态加载字段候选值，而不是长期写死在前端。
- 接入密钥管理、固定出口 IP、请求日志脱敏、幂等和限流队列。
- 生产环境强制设置 `PUBLIC_BASE_URL`、`DINGTALK_CLIENT_SECRET` 和 `DINGTALK_FORM_SECRET`，并使用 HTTPS。
- “实际现象”支持最多 5 个截图/视频，单个不超过 8MB、合计不超过 12MB；媒体会保存到 `MEDIA_DIR`，以 HTML 图片/链接写入 TAPD 描述，并在创建 Bug 成功后逐个上传到 TAPD 附件接口。
- 可以直接把系统截图复制后粘贴到“实际现象”文字框，浏览器会自动加入媒体列表；文件选择按钮保留作备用。
- Bug Agent 模式下，单聊或目标群 @缺陷机器人后直接发送文字、图片/视频即可触发分析；默认由服务端自动创建 Bug，无需确认字段。
- 视频会由服务器抽取最多 4 帧发送给视觉模型，原视频仍会保留为草稿中的链接。
- “模块”“发现版本”“迭代”“发布计划”从 TAPD `/bugs/get_fields_info`、`/iterations` 和 `/releases` 动态加载，并将选择值分别写入 `module`、`version_report`、`iteration_id`、`release_id`。
- 自动建单默认迭代为 `企业知识中心9月`、发现版本为 `v1.3.0`、模块为 `企业知识中心`、测试方式为 `手工测试`、迭代需求缺陷为“是”；默认值只有命中 TAPD 候选列表时才写入，发布计划按发布时间等日期字段倒序并默认选择最新一条。
- 优先级、严重程度、缺陷根源、测试方式和迭代需求缺陷均优先读取 TAPD 字段配置候选，再由模型按问题内容选择合法值；缺陷根源可通过 `TAPD_DEFAULT_SOURCE` 设置兜底值。
- “处理人”从 TAPD `/workspaces/users` 动态加载，界面显示姓名和账号，提交时写入 TAPD 需要的 `current_owner` 用户名。
- “处理人”支持输入姓名/账号并从下拉候选中选择；服务端使用钉钉 `msgId` 在短时间窗口内去重，避免重复回复卡片。
- 自动模式按 AI 解析出的模块、标题和描述匹配责任规则；默认编译/对话部分分配给杨耀发，抽取/本体分配给肖文杨，并将处理人/开发人解析为 TAPD 项目成员账号。配置 `TAPD_RESPONSIBILITY_WHITELIST` 后覆盖默认分工，未命中时使用 `TAPD_DEFAULT_OWNER` 和 `TAPD_DEFAULT_DEVELOPER`。当前所有自动建单的测试人统一为 `TAPD_DEFAULT_TESTER`（默认 `雷艾琳`）。
- 当前表单不展示“发现阶段”“软件平台”；“发布计划”可从 TAPD 动态读取并选择。
- 当前媒体链接是随机文件名但未接入登录鉴权，仅适合内网/PoC；正式环境应使用固定域名、对象存储和带签名的访问链接。
- 钉钉图片/视频回调中的 `downloadCode` 需要通过钉钉文件下载接口换取临时下载地址；企业应用需要 `DINGTALK_APP_KEY` 和对应 Secret（默认复用 `DINGTALK_CLIENT_SECRET`）。
- 配置的 `OPENAI_MODEL` 负责分析媒体和生成字段候选；默认值为 `gpt-5.6-sol`。服务端会校验责任人后自动执行 TAPD 创建。将 `TAPD_AUTO_CREATE_BUGS` 设为 `false` 可恢复 `/api/drafts/{id}/confirm` 人工确认模式。
- 如果配置 `DINGTALK_CARD_TEMPLATE_ID`、`DINGTALK_CARD_CALLBACK_ROUTE_KEY`、`DINGTALK_CARD_CALLBACK_SECRET`，草稿可投放为钉钉互动卡片，确认/取消直接在钉钉窗口内完成；未配置时继续使用 H5 草稿页兜底。
- TAPD 附件上传使用 `/files/upload_attachment` 的 multipart 请求。默认 `TAPD_ATTACHMENT_TYPE=bug` 且不传 `TAPD_ATTACHMENT_CUSTOM_FIELD`，这样会写入标准 Bug“附件”区域并显示图片；只有需要上传到 Bug 自定义附件字段时才设置 `TAPD_ATTACHMENT_CUSTOM_FIELD`。上传失败时 Bug 仍会创建成功，响应和卡片状态会列出失败附件。
- Bug 标题会在 AI 草稿、卡片编辑和最终提交时统一规范为 `【模块名称】具体问题描述`；自动默认前缀为 `【企业知识中心—用户反馈】`，未选择模块时使用 `【待确认模块】` 占位。

### 窗口内确认配置

权限直达：[TAPD 应用权限](https://open.tapd.cn/admin/5276/permission)、[钉钉开发者平台](https://open-dev.dingtalk.com/)。

普通 Markdown/ActionCard 的按钮只能打开 URL；要在钉钉聊天窗口里直接点击“确认创建”，必须创建并发布一个互动卡片模板，并给模板配置这些动态参数：

```text
title
module
version_report
iteration
release_plan
priority_label
severity
description
confidence
media_count
draft_id
status
status_detail
bug_url
statusText
statusDetail
attachmentStatus
```

给模板增加两个“回传请求”按钮：

```text
确认创建：action=confirm，draft_id={{draft_id}}
取消：action=cancel，draft_id={{draft_id}}
```

模板中还需要增加一个 Markdown 或文本组件，将 `statusText`（或 `statusDetail`）绑定为内容；“确认创建”按钮的文案/状态可绑定 `submitText`、`submitButtonStatus`，按钮可见性绑定 `confirmButtonVisible` 和 `cancelButtonVisible`。你提供的最新导出 JSON 已声明这些状态变量，但目前没有状态组件，需在模板编辑器中补上后重新发布。已生成可直接导入再发布的示例：`work/bug-agent-card-template-status.json`。

模板发布后，获得模板 ID。在服务器执行：

```bash
/opt/dingtalk-tapd-bug-bot/configure-interactive-card.sh
```

脚本会写入模板 ID、RouteKey、回调 Secret，注册回调地址 `https://你的公网地址/dingtalk/card-callback`，并重启服务。互动卡片还需要在钉钉应用权限中开启“互动卡片实例写权限”。

## 6. 已部署服务器信息

当前 PoC 已独立部署到 `10.201.0.151`：

```text
应用目录：/opt/dingtalk-tapd-bug-bot
systemd：dingtalk-tapd-bug-bot.service
监听端口：3000
环境文件：/etc/dingtalk-tapd-bug-bot.env（权限 600）
```

常用运维命令：

```bash
systemctl status dingtalk-tapd-bug-bot.service
systemctl restart dingtalk-tapd-bug-bot.service
journalctl -u dingtalk-tapd-bug-bot.service -f
curl http://127.0.0.1:3000/healthz
```

服务器当前使用空配置等待真实凭据。配置 TAPD 和钉钉参数时，编辑 `/etc/dingtalk-tapd-bug-bot.env` 后执行重启。`10.201.0.151` 是私网地址，不能直接作为钉钉回调地址，必须通过公网 HTTPS 反向代理或公网隧道暴露。

Bug Agent 还需要配置 `OPENAI_API_KEY`、`OPENAI_BASE_URL` 和 `OPENAI_MODEL`。中转站场景示例：

```text
OPENAI_BASE_URL=https://dts.deepexi.com/v1
OPENAI_MODEL=gpt-5.6-sol
OPENAI_API_MODE=responses
```

`OPENAI_API_MODE=auto` 会先尝试 Responses API，遇到 404/405/不支持时回退到 Chat Completions；如果中转站明确兼容 Responses，建议固定为 `responses`。

ChatGPT/Codex 中可用某个模型，不等于中转站 API 自动拥有该模型权限；以中转站“获取模型列表”显示的模型 ID 为准。

服务器已提供交互式配置脚本，直接运行即可，不需要手工编辑环境文件：

```bash
/opt/dingtalk-tapd-bug-bot/configure-bug-agent.sh
```

脚本会隐藏输入 OpenAI API Key 和 TAPD 应用密钥，并写入 root-only 配置后重启服务。
