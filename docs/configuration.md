# Dingtalk TAPD Bridge 配置说明

本文是项目的配置单一入口，面向部署人员和需要二次开发的开发人员。项目包含两个可独立运行的进程：

| 进程 | 目录 | 职责 |
| --- | --- | --- |
| Python Bridge | 仓库根目录 | 调用 DWS 读取群消息，生成/创建 TAPD Bug，维护事件去重账本 |
| Node Bug Agent | `services/dingtalk-tapd-bug-bot` | 接收钉钉回调，下载媒体，调用模型分析并按配置自动创建 TAPD Bug |

两条链路可以单独使用，也可以通过 HMAC 签名的 `/api/agent/events` 连接：Python 负责监听目标群，Node 负责模型分析、责任人匹配和 TAPD 写入。

## 1. 配置注入

### 本地 Python Bridge

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .

# DWS 认证由 dws CLI 管理
dws auth login

# TAPD MCP 默认从当前进程继承令牌
export TAPD_ACCESS_TOKEN='仅在本机环境设置'
```

Python CLI 默认使用 `TAPD_BACKEND=mcp`，并执行 `uvx mcp-server-tapd`。只有明确设置 `TAPD_BACKEND=rest` 时才使用内置 REST 客户端。

### Node Bug Agent

Node 服务没有引入 `dotenv`，`.env` 不会被自动读取。开发环境可以这样加载：

```bash
cd services/dingtalk-tapd-bug-bot
cp .env.example .env
set -a
. .env
set +a
node server.js
```

生产环境使用 systemd 的 `EnvironmentFile`，推荐文件位置为 `/etc/dingtalk-tapd-bug-bot.env`，权限设为 `600`，不要把真实 `.env` 放进 Git。

## 2. Python Bridge 环境变量

以下变量由 `src/dingtalk_tapd/config.py` 读取。

| 变量 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `DWS_EXECUTABLE` | 否 | `dws` | DWS 可执行文件路径或名称 |
| `DWS_PROFILE` | 否 | 当前 profile | 指定钉钉组织/账号 profile |
| `TAPD_BACKEND` | 否 | `mcp` | `mcp` 或 `rest` |
| `TAPD_MCP_COMMAND` | 否 | `uvx mcp-server-tapd` | MCP 启动命令，按 shell 规则拆分 |
| `TAPD_ACCESS_TOKEN` | REST/服务需要 | 空 | TAPD OAuth access token；优先级高于 Basic 认证 |
| `TAPD_API_USER` | 否 | 空 | REST Basic 认证用户名 |
| `TAPD_API_PASSWORD` | 否 | 空 | REST Basic 认证密码 |
| `TAPD_API_BASE_URL` | 否 | `https://api.tapd.cn` | TAPD API 地址 |
| `TAPD_BASE_URL` | 否 | `https://www.tapd.cn` | TAPD 页面地址，传给 MCP 生成链接 |
| `DINGTALK_TAPD_GROUP_ID` | 自动流程需要 | `cid3SbKZNiotRpk9RdlluSUSA==` | 目标群的稳定 `openConversationId` |
| `DINGTALK_TAPD_GROUP_NAME` | 否 | `DeepWorks 产品交流群` | 写入来源描述时展示的群名 |
| `DINGTALK_TAPD_WORKSPACE_ID` | 自动流程需要 | `57379524` | 自动建单的 TAPD 项目 ID |
| `DINGTALK_TAPD_OWNER` | 否 | `雷艾琳` | 自动建单负责人 |
| `DINGTALK_TAPD_DEVELOPER` | 否 | 空 | 未命中白名单时的默认开发人 |
| `DINGTALK_TAPD_TESTER` | 否 | `雷艾琳` | 自动建单测试人；当前不会按模块切换 |
| `DINGTALK_TAPD_RESPONSIBILITY_WHITELIST` | 否 | 空 | JSON 规则；按消息/模块关键词选择负责人和开发人 |
| `DINGTALK_TAPD_TITLE_PREFIX` | 否 | `【用户反馈】` | 自动 Bug 标题前缀 |
| `DINGTALK_TAPD_STATE_DB` | 否 | `.dingtalk-tapd/state.sqlite3` | 事件幂等和失败记录的 SQLite 文件 |
| `DINGTALK_TAPD_ATTACHMENT_DIR` | 否 | `.dingtalk-tapd/attachments` | DWS 下载资源目录，必须是工作目录内相对路径 |
| `DINGTALK_TAPD_READY_TIMEOUT` | 否 | `30` | DWS 实时监听等待 ready 的秒数 |
| `DINGTALK_TAPD_OCR_COMMAND` | 否 | 自动探测 `tesseract` | 自定义 OCR 命令模板；使用 `{path}` 代表图片路径 |
| `DINGTALK_TAPD_MENTION_TARGETS` | 否 | `董超,买年顺` | 普通 `listen` 放行的 @姓名，逗号分隔 |
| `DINGTALK_TAPD_MENTION_TARGET_IDS` | 否 | 空 | 普通 `listen` 放行的稳定用户 ID，逗号分隔 |
| `DINGTALK_TAPD_BOT_MENTION_TARGETS` | `agent-listen` 否 | `缺陷机器人` | Bug Agent 监听放行的 @姓名 |
| `DINGTALK_TAPD_BOT_MENTION_TARGET_IDS` | `agent-listen` 否 | 内置缺陷机器人 ID | Bug Agent 监听放行的稳定用户 ID |
| `DINGTALK_TAPD_AGENT_EVENTS_URL` | `agent-listen` 是 | 空 | Node 的 `/api/agent/events` 地址 |
| `DINGTALK_TAPD_AGENT_SECRET` | `agent-listen` 是 | 空 | 与 Node 的 `AGENT_INGEST_SECRET` 完全一致 |
| `DINGTALK_TAPD_AGENT_TIMEOUT` | 否 | `30` | 转发 Node 事件的 HTTP 超时秒数 |

`DINGTALK_TAPD_ATTACHMENT_DIR` 会拒绝绝对路径和包含 `..` 的路径。这样可以避免 DWS 下载资源逃逸到工作目录之外。

## 3. Node Bug Agent 环境变量

以下变量由 `services/dingtalk-tapd-bug-bot/server.js` 读取。

### 服务与媒体

| 变量 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `PORT` | 否 | `3000` | HTTP 监听端口 |
| `PUBLIC_BASE_URL` | 生产必填 | 空 | 钉钉能访问的公网 HTTPS 根地址；用于回调、草稿和媒体链接 |
| `MEDIA_DIR` | 否 | 服务目录下 `media` | 媒体落盘目录；systemd 部署建议使用 `/var/lib/dingtalk-tapd-bug-bot/media` |

### TAPD

| 变量 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `TAPD_WORKSPACE_ID` | 创建 Bug 必填 | 空 | Node 服务固定写入的 TAPD 项目 ID |
| `TAPD_API_BASE_URL` | 否 | `https://api.tapd.cn` | TAPD API 地址 |
| `TAPD_ACCESS_TOKEN` | 三选一 | 空 | 静态 OAuth token，优先使用 |
| `TAPD_CLIENT_ID` | OAuth 二选一 | 空 | TAPD 应用 ID |
| `TAPD_CLIENT_SECRET` | OAuth 二选一 | 空 | TAPD 应用密钥，服务会自动刷新 token |
| `TAPD_API_USER` | Basic 二选一 | 空 | 兼容模式用户名，不建议生产使用 |
| `TAPD_API_PASSWORD` | Basic 二选一 | 空 | 兼容模式密码 |
| `TAPD_ATTACHMENT_TYPE` | 否 | `bug` | TAPD 附件类型；标准 Bug 附件保持 `bug` |
| `TAPD_ATTACHMENT_CUSTOM_FIELD` | 否 | 空 | 只有上传到 Bug 自定义附件字段时才填写 |
| `TAPD_ATTACHMENT_OWNER` | 否 | 空 | TAPD 附件接口的可选 owner |
| `TAPD_AUTO_CREATE_BUGS` | 否 | `true` | 自动入口是否直接建单；设为 `false` 才保留草稿并等待人工确认 |
| `TAPD_DEFAULT_OWNER` | 否 | `雷艾琳` | Node 自动模式未命中白名单时的默认负责人 |
| `TAPD_DEFAULT_DEVELOPER` | 否 | 空 | Node 自动模式未命中白名单时的默认开发人 |
| `TAPD_DEFAULT_TESTER` | 否 | `雷艾琳` | Node 自动模式统一写入的测试人 |
| `TAPD_RESPONSIBILITY_WHITELIST` | 否 | 空 | Node 自动模式的模块/关键词责任人 JSON 规则 |
| `TAPD_DEFAULT_PRIORITY_LABEL` | 否 | `中` | 表单未选择优先级时的默认值 |
| `TAPD_BUG_URL_TEMPLATE` | 否 | `https://www.tapd.cn/{workspace_id}/bugtrace/bugs/view?bug_id={id}` | 创建成功后的链接模板，必须保留两个占位符 |

认证优先级是 `TAPD_ACCESS_TOKEN`，其次是 `TAPD_CLIENT_ID`/`TAPD_CLIENT_SECRET`，最后才是 `TAPD_API_USER`/`TAPD_API_PASSWORD`。OAuth token 过期时，服务会用应用凭据刷新一次。

### 钉钉回调、媒体与互动卡片

| 变量 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `DINGTALK_CLIENT_SECRET` | 回调验签建议必填 | 空 | 钉钉机器人回调签名密钥；生产必须配置 |
| `DINGTALK_APP_KEY` | 媒体/卡片需要 | 空 | 钉钉企业应用 AppKey |
| `DINGTALK_APP_SECRET` | 媒体/卡片需要 | 使用 `DINGTALK_CLIENT_SECRET` | 企业应用 Secret，建议单独设置 |
| `DINGTALK_ROBOT_CODE` | 媒体/群通知需要 | 空 | 机器人 RobotCode |
| `DINGTALK_ACCESS_TOKEN` | 否 | 空 | 静态钉钉 access token；未设置时用 AppKey/Secret 动态获取 |
| `DINGTALK_API_BASE_URL` | 否 | `https://api.dingtalk.com` | 钉钉 OpenAPI 地址 |
| `DINGTALK_API_RETRY_ATTEMPTS` | 否 | `3` | 钉钉 API 临时失败的最大尝试次数 |
| `DINGTALK_FORM_SECRET` | 生产必填 | 空 | H5 表单状态签名密钥；建单状态默认 15 分钟过期 |
| `DINGTALK_CARD_TEMPLATE_ID` | 互动卡片三项必填 | 空 | 已发布的卡片模板 ID |
| `DINGTALK_CARD_CALLBACK_ROUTE_KEY` | 互动卡片三项必填 | 空 | 卡片回调 RouteKey |
| `DINGTALK_CARD_CALLBACK_SECRET` | 互动卡片三项必填 | 空 | 卡片回调验签密钥 |
| `DINGTALK_CARD_REVIEW_RECIPIENT_ID` | 否 | `641447065` | 互动卡片私投的稳定用户 ID，默认是雷艾琳 |

互动卡片只有在模板 ID、RouteKey、Secret 三项同时存在时才启用；任一项缺失会回退到草稿 H5 链接。卡片确认回调地址固定为 `${PUBLIC_BASE_URL}/dingtalk/card-callback`。

### Bug Agent 与模型

| 变量 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `ENABLE_BUG_AGENT` | 否 | `true` | 设为 `false` 禁用媒体分析入口 |
| `OPENAI_API_KEY` | Bug Agent 必填 | 空 | 模型服务密钥 |
| `OPENAI_BASE_URL` | 否 | `https://api.openai.com/v1` | OpenAI 或兼容中转站地址 |
| `OPENAI_MODEL` | 否 | `gpt-5.6-sol` | 以实际 API 账户可用的模型 ID 为准 |
| `OPENAI_API_MODE` | 否 | `auto` | `auto`、`responses` 或 `chat_completions` |
| `OPENAI_TIMEOUT_MS` | 否 | `90000` | 模型请求超时毫秒数 |
| `OPENAI_MAX_OUTPUT_TOKENS` | 否 | `1800` | 模型草稿最大输出 token 数 |
| `MOCK_TAPD` | 本地验证可用 | `false` | `true` 时不请求 TAPD，只返回模拟 Bug ID |

`auto` 模式先调用 Responses API；遇到 400/404/405/501 或明确的“不支持”错误时回退到 Chat Completions。中转站若只支持其中一种模式，建议显式设置。

自动模式会先读取 TAPD 项目成员，再按白名单把姓名解析为成员账号；未命中规则时使用 `TAPD_DEFAULT_OWNER`、`TAPD_DEFAULT_DEVELOPER` 和 `TAPD_DEFAULT_TESTER`。如果希望人工检查模型草稿，把 `TAPD_AUTO_CREATE_BUGS` 设为 `false`。

责任白名单支持“数组”或“模块名到责任人对象”的 JSON。`match`/`module`/`keywords` 用于包含匹配，`*`、`default` 或 `默认` 表示默认规则：

```json
[
  {"match": "企业知识中心", "owner": "负责人账号", "developer": "开发人账号"},
  {"match": "*", "owner": "默认负责人账号", "developer": "默认开发人账号"}
]
```

Python 使用 `DINGTALK_TAPD_RESPONSIBILITY_WHITELIST`、`DINGTALK_TAPD_OWNER` 等变量；Node 使用 `TAPD_RESPONSIBILITY_WHITELIST`、`TAPD_DEFAULT_OWNER` 等变量。两端规则格式相同，但配置不会自动跨进程读取。

### Python 到 Node 的桥接

两端必须使用同一随机密钥：

```bash
# Python 监听器
export DINGTALK_TAPD_AGENT_EVENTS_URL='https://bug.example.com/api/agent/events'
export DINGTALK_TAPD_AGENT_SECRET='随机生成的 32 位以上密钥'

# Node 服务
AGENT_INGEST_SECRET='同一密钥'
```

请求使用 `x-agent-timestamp` 和 `x-agent-signature` 两个请求头，签名内容为 `timestamp + "\n" + 原始 JSON 请求体` 的 HMAC-SHA256 十六进制值。时间戳超过 5 分钟或 `eventId` 在 30 分钟内重复时，服务会拒绝或返回 `duplicate`。

## 4. 钉钉与 TAPD 权限

最小权限清单如下：

| 系统 | 需要的能力 |
| --- | --- |
| DWS | 当前 profile 能登录并读取目标群；实时模式需要事件订阅权限 |
| 钉钉企业应用 | 机器人回调、媒体下载、机器人发群消息；互动卡片还需卡片实例写权限 |
| TAPD | 目标 workspace 的 Bug 创建、字段读取、成员读取和附件上传 |
| 模型服务 | 对 `OPENAI_MODEL` 对应模型的图像输入和 JSON 输出权限 |

钉钉回调地址必须从公网 HTTPS 可达。`10.201.0.151` 这类私网地址不能直接填入钉钉后台，需要反向代理或公网隧道。

## 5. 数据与生命周期约束

- Python 事件键是 `conversationId:messageId`，存于 `DINGTALK_TAPD_STATE_DB`；已创建、已忽略和失败都会留账。
- Node 的待确认草稿只保存在内存，默认 30 分钟过期；服务重启会丢失未确认草稿。
- H5 会话状态默认 15 分钟过期；过期后必须从钉钉重新打开。
- 媒体最多 5 个，单个最多 8 MB，总计最多 12 MB；视频最多抽取 4 帧供模型分析。
- TAPD Bug 创建成功后，附件逐个上传；附件上传失败不会回滚已创建的 Bug，响应和卡片会列出失败项。
- 本地 DWS 下载文件和 Node `MEDIA_DIR` 文件都可能包含业务敏感信息，应限制目录权限并规划清理策略。

## 6. 二次开发入口

### Python

| 文件 | 适合修改的内容 |
| --- | --- |
| `src/dingtalk_tapd/config.py` | 环境变量、默认目标、认证配置和路径校验 |
| `src/dingtalk_tapd/dws.py` | DWS 命令参数、群解析、消息分页和详情下载 |
| `src/dingtalk_tapd/realtime.py` | 实时事件订阅、@过滤和进程生命周期 |
| `src/dingtalk_tapd/automation.py` | 相关性判断、优先级、标题、OCR、幂等和自动建单 |
| `src/dingtalk_tapd/orchestrator.py` | 手工检索、草稿、字段校验和写入确认门禁 |
| `src/dingtalk_tapd/mcp.py` / `tapd.py` | TAPD MCP/REST 适配层 |
| `src/dingtalk_tapd/agent.py` | 事件媒体编码、HMAC 签名和远端转发 |
| `src/dingtalk_tapd/store.py` | SQLite 事件状态和失败重试策略 |

### Node

| 位置 | 适合修改的内容 |
| --- | --- |
| `getConfig()` | 新增环境变量时的默认值和归一化 |
| `analyzeBugWithOpenAI()` | 提示词、模型协议和草稿 JSON 字段 |
| `normalizeAgentDraft()` | 模型输出到 TAPD 字段的白名单映射 |
| `getTapdOptions()` | 动态读取模块、版本、迭代、发布计划和处理人 |
| `createConfirmedDraft()` / `handleConfirmDraft()` | 人工确认后的 TAPD 写入边界 |
| `uploadTapdAttachments()` | 附件大小、字段和失败处理 |
| `createAndDeliverInteractiveDraft()` / `updateInteractiveCard()` | 卡片变量、私投和串行更新 |
| `createServer()` | HTTP 路由和鉴权入口 |

扩展字段时要同步更新模型 JSON 字段、`normalizeAgentDraft`、责任人解析、卡片参数映射、表单提交和 TAPD payload；不要只改前端显示名。自动建单必须沿用白名单、签名和幂等约束；人工回退模式则继续放在确认按钮之后，并保留来源 `conversationId`、`messageId` 和幂等键。

## 7. 安全底线

1. 真实 token、密码、AppSecret、HMAC 密钥只放环境变量或密钥管理系统，不写入 README、日志和 JSON 响应。
2. 生产必须设置 `PUBLIC_BASE_URL`、`DINGTALK_CLIENT_SECRET`、`DINGTALK_FORM_SECRET` 和 `AGENT_INGEST_SECRET`（启用桥接时）。
3. 不要把 `MOCK_TAPD=true` 带到生产；它只用于本地验证页面和流程。
4. 手工 CLI 创建前必须有 `--confirm`；`partial` 检索结果必须显式使用 `--allow-partial`。
5. 新增定时任务或批量写入时，先复用现有事件账本，避免重复创建 TAPD 工单。
