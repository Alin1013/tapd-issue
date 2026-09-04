# 钉钉 DWS CLI 与 TAPD MCP 集成研究

> 本报告记录对两个项目官方 README、Skill、源码和命令契约的核查，并说明如何在 Codex 中编排“检索 Deepworks 问题群对话并创建 TAPD 工单”。截至 2026-09-04，本次已安装 DWS CLI 并注册 TAPD MCP 配置，但尚未登录钉钉或访问业务数据。

## 结论

**可以通过 Codex 的自然语言完成这条流程，但两个项目之间没有现成的内置连接器。** 推荐的边界是：

1. `dws` 负责钉钉认证、群名解析和聊天消息查询；DWS 的 `dingtalk-chat` Skill 会把这些 CLI 能力暴露给 Agent。
2. `mcp-server-tapd` 作为独立 MCP Server 负责 TAPD 项目解析、字段查询和创建需求/任务/缺陷。
3. Codex 读取 DWS 的 JSON 结果，整理问题事实，在写 TAPD 前向用户确认目标项目、工单类型和字段，然后调用 TAPD MCP 的写工具。

因此，“一次交互中自然语言找群聊并建单”可行；“无人确认、定时持续扫描并去重建单”还需要额外的调度、事件监听和幂等存储，本次两个仓库没有直接提供完整方案。

## 官方来源

以下均为上游项目的官方仓库或官方文档，链接固定到 `main` 分支的文件：

| 来源 | 用途 |
| --- | --- |
| [DWS README_zh.md](https://github.com/DingTalk-Real-AI/dingtalk-workspace-cli/blob/main/README_zh.md) | 安装方式、认证、Skill 与 Agent 使用说明 |
| [DWS dingtalk-chat/SKILL.md](https://github.com/DingTalk-Real-AI/dingtalk-workspace-cli/blob/main/skills/multi/dingtalk-chat/SKILL.md) | 群聊/消息的 Agent 路由和安全契约 |
| [DWS message-query reference](https://github.com/DingTalk-Real-AI/dingtalk-workspace-cli/blob/main/skills/multi/dingtalk-chat/references/chat/message-query.md) | 指定会话读取、关键词搜索、时间范围和分页语义 |
| [DWS group-discovery reference](https://github.com/DingTalk-Real-AI/dingtalk-workspace-cli/blob/main/skills/multi/dingtalk-chat/references/chat/group-discovery.md) | 按群名搜索与稳定 `openConversationId` 规则 |
| [DWS 01-messaging reference](https://github.com/DingTalk-Real-AI/dingtalk-workspace-cli/blob/main/skills/multi/dingtalk-chat/references/01-messaging.md) | 跨步骤传递群 ID、消息 ID 和完整性字段 |
| [DWS install.sh](https://github.com/DingTalk-Real-AI/dingtalk-workspace-cli/blob/main/scripts/install.sh) | 安装器默认路径、版本与校验行为 |
| [TAPD MCP README.md](https://github.com/TencentCloudCommunity/mcp-server/blob/main/src/mcp-server-tapd/README.md) | TAPD MCP 安装、环境变量和 MCP 配置 |
| [TAPD pyproject.toml](https://github.com/TencentCloudCommunity/mcp-server/blob/main/src/mcp-server-tapd/pyproject.toml) | Python 版本、依赖和 `mcp-server-tapd` 入口 |
| [TAPD server.py](https://github.com/TencentCloudCommunity/mcp-server/blob/main/src/mcp-server-tapd/src/mcp_server_tapd/server.py) | MCP 工具签名与创建/查询逻辑 |
| [TAPD app_config.py](https://github.com/TencentCloudCommunity/mcp-server/blob/main/src/mcp-server-tapd/src/mcp_server_tapd/app_config.py) | CLI 参数、环境变量和优先级 |
| [TAPD tapd.py](https://github.com/TencentCloudCommunity/mcp-server/blob/main/src/mcp-server-tapd/src/mcp_server_tapd/tapd.py) | Bearer/Basic 认证和 TAPD API 请求实现 |
| [Codex MCP command source](https://github.com/openai/codex/blob/main/codex-rs/cli/src/mcp_cmd.rs) | `codex mcp add/list/get/remove` 的官方实现与参数 |
| [Codex MCP CLI tests](https://github.com/openai/codex/blob/main/codex-rs/cli/tests/mcp_add_remove.rs) | stdio MCP 与 `--env KEY=VALUE` 配置行为 |

## DWS CLI：安装与能力

### 安装方式

DWS README 的 macOS/Linux 快速安装是不需要 Go 或 Node.js 的预编译安装器：

```bash
curl -fsSL https://raw.githubusercontent.com/DingTalk-Real-AI/dingtalk-workspace-cli/main/scripts/install.sh | sh
```

README 同时列出 npm、Homebrew、GitHub Releases 和源码构建方式；源码构建要求 Go 1.25+。中国大陆网络可将安装源切换为 Gitee：

```bash
DWS_GITEE_REPO=DingTalk-Real-AI/dingtalk-workspace-cli \
  curl -fsSL https://gitee.com/DingTalk-Real-AI/dingtalk-workspace-cli/raw/main/scripts/install.sh | sh
```

安装器的环境变量包括 `DWS_INSTALL_DIR`、`DWS_VERSION`、`DWS_NO_SKILLS`、`DWS_SKILLS_ONLY` 和 `DWS_SKILL_MODE`；默认非交互安装使用 `multi` Skill 布局。README 说明安装器会优先检测具体 Agent 的 Skill 根目录（包括 Codex），也可以显式执行：

```bash
dws skill setup --mode multi --target codex --dry-run
dws skill setup --mode multi --target codex
```

源码的 `skill_setup.go` 将 `~/.codex/skills` 列为兼容目标，并提供 `--target codex`。`dingtalk-chat` Skill 是 `multi` 布局中的产品 Skill；只安装聊天能力时，可在支持的版本上选择对应 Skill，避免将全部产品文档加入 Agent 上下文。

按需只铺设聊天 Skill（`--skill` 可重复传入多个产品）时，可使用：

```bash
dws skill setup --mode multi --target codex --skill dingtalk-chat --dry-run
dws skill setup --mode multi --target codex --skill dingtalk-chat
```

### 认证和运行前提

首次使用：

```bash
dws auth login          # 浏览器 OAuth
dws auth login --device # 无头环境或 SSH
```

README 要求企业管理员在钉钉开放平台启用 CLI Access；若组织尚未启用，登录流程会引导申请。DWS 支持多个组织/账号 profile，跨组织读取应先用 `dws profile list` 确认 `isOrgCurrent=true` 的账号；写操作默认只作用于当前账号。

DWS 对 Agent 友好：业务命令可用 `--format json`，`--dry-run` 预览，`--jq` 精确抽取；`dws schema` 只描述命令契约，不读取业务数据。认证令牌默认使用系统 Keychain（Linux 使用文件存储）；README 特别说明 Codex App 等 macOS 沙盒若无法调用 Keychain，可设置 `DWS_DISABLE_KEYCHAIN=1`，但这会降低静态存储保护强度。

### 检索群与消息

官方 Skill 的 Golden Route 是：

1. 群名/群 ID 解析：`dws chat +chat-search --query <群名> --page-all --format json`。
2. 已知群内按条件检索：`dws chat +search-msg --chat-query <群名> --query <关键词> --page-all --format json`。
3. 只需读取指定群记录：`dws chat +chat-messages --group <群名或openConversationId> --page-all --format json`。

可执行示例（关键词和时间按业务替换）：

```bash
dws chat +search-msg \
  --chat-query "Deepworks问题群" \
  --query "问题" \
  --start "2026-09-01T00:00:00+08:00" \
  --end "2026-09-05T00:00:00+08:00" \
  --order asc --page-all --format json
```

需要注意的官方契约：

- `--chat-query` 会按群名解析稳定会话；零命中或多候选必须停止并让用户消歧，不能选第一项。
- 未指定会话时消息搜索默认只查最近 7 天；要做“某个时间段全部问题”，应明确传 `--start/--end`。
- `--page-all` 才会沿真实游标翻页；必须检查 `complete`、`hasMore`、`nextCursor`/`nextPage`、`stopReason` 和 `failures`。部分结果不能表述成完整历史。
- `--sender-query`、`--query`、消息类型等过滤应由 CLI 解析真实 user ID 或会话 ID；不要让模型从展示名猜 ID。
- 返回中的 `messageId`、`conversationId`、`resourceRefs` 可用于后续引用、下载或保存来源；`openTaskId` 是发送任务 ID，不是消息 ID。

这证明 DWS 能完成“在用户有权限看到的 Deepworks 群里按关键词和时间找对话”，但不能绕过钉钉企业权限，也不能保证查到用户不可见或未被搜索授权覆盖的历史。

## TAPD MCP：安装、配置与工具

### 安装和凭据

TAPD README 要求 `uv`；`pyproject.toml` 声明 Python `>=3.13`，依赖 `mcp[cli]`、`requests`、`markdown` 等，入口脚本为 `mcp-server-tapd = mcp_server_tapd.server:main`。Codex 使用 stdio 时可让 `uvx` 按需运行 PyPI 包：

```bash
uvx mcp-server-tapd
```

推荐 TAPD 个人令牌：

```text
TAPD_ACCESS_TOKEN=<个人令牌>
TAPD_API_BASE_URL=https://api.tapd.cn
TAPD_BASE_URL=https://www.tapd.cn
```

`app_config.py` 还支持 `TAPD_API_USER`/`TAPD_API_PASSWORD` 的 Basic 认证兼容模式，以及可选 `BOT_URL`。`tapd.py` 的实现明确优先使用 `TAPD_ACCESS_TOKEN`，否则使用 API 用户名/密码；不要在报告、仓库或聊天中写入真实令牌。

### 在 Codex 注册 MCP

官方 TAPD README 给出 `mcpServers` 的 stdio 配置；官方 Codex CLI 源码提供等价的 `codex mcp add <name> --env KEY=VALUE -- <command>`。示例中的值必须替换为本地安全注入的凭据：

```bash
codex mcp add mcp-server-tapd \
  --env TAPD_ACCESS_TOKEN=YOUR_TAPD_ACCESS_TOKEN \
  --env TAPD_API_BASE_URL=https://api.tapd.cn \
  --env TAPD_BASE_URL=https://www.tapd.cn \
  -- uvx mcp-server-tapd
```

`codex mcp list` / `codex mcp get mcp-server-tapd --json` 可检查配置。注意 `--env` 的值会写入 Codex 的 MCP 配置；生产环境应考虑使用 Codex 进程继承的环境变量、受保护的配置文件或外部 Secret 注入，避免令牌明文长期落盘。

也可以按 TAPD README 使用 `~/.codex/config.toml` 对应的 stdio 配置，或先启动 `streamable-http` 服务，再在 Codex 中添加 `--url http://localhost:8000/mcp/`。本地 stdio 路径更少，适合先验证。

### 可调用的 TAPD 工具

`server.py` 使用 FastMCP 注册了下列与建单直接相关的工具：

| 工具 | 用途 |
| --- | --- |
| `get_user_participant_projects` | 按用户昵称找参与的 TAPD 项目；当用户未给 `workspace_id` 时用于消歧 |
| `get_workspace_info` | 查询项目基本信息 |
| `get_entity_custom_fields` | 创建或查询前读取需求/任务/迭代/用例自定义字段配置 |
| `create_bug` | 创建 TAPD 缺陷，必填 `workspace_id`、`title`，可带描述、优先级、负责人、严重程度、自定义字段和图片/视频直链 |
| `create_story_or_task` | 创建需求或任务，必填 `workspace_id`、`name`，`options.entity_type` 选择 `stories` 或 `tasks` |
| `create_comments` | 给已有缺陷、需求或任务添加评论 |
| `get_bug` / `get_stories_or_tasks` | 写入后查询或核对结果 |

创建逻辑会返回 TAPD URL/数据；当描述含 Markdown 时，服务端会转换为 HTML，支持外部图片或视频直链。自定义字段必须先读取配置，避免把字段别名或候选值猜错。

## Codex 中的自然语言编排方案

建议将一次用户请求拆成“只读检索 → 摘要确认 → 单次写入”三段，避免在还没有确定来源和目标项目时产生副作用：

```text
用户：找 Deepworks 问题群里最近一周关于登录失败的对话，整理成 TAPD 缺陷
  │
  ├─ DWS：按群名唯一解析 + 按关键词/时间搜索（JSON）
  │       检查 complete/hasMore/failures，保留 messageId 和时间
  │
  ├─ Codex：去重、提取标题/现象/复现步骤/期望/实际/来源消息
  │       查询 TAPD workspace，必要时查询自定义字段
  │
  ├─ 用户确认：目标 workspace、缺陷还是需求、标题、负责人/优先级
  │
  └─ TAPD MCP：create_bug 或 create_story_or_task
          返回工单 URL、TAPD ID 和来源摘要
```

推荐给 Codex 的自然语言约束可以写成：

> 先在当前钉钉 profile 中唯一定位“Deepworks 问题群”，检索指定时间段内包含“登录失败”的消息；只读阶段结束后展示候选消息、完整性字段和拟建 TAPD 项目。没有用户确认前不要调用任何 TAPD 写工具。确认后创建一个 TAPD 缺陷，描述中保留消息时间、发送者和 `messageId`，并返回 TAPD 链接。

这里的“自然语言”来自 DWS 安装的 Agent Skill 和 Codex 对 MCP 工具的调用能力；DWS 本身不是一个把对话自动转成 TAPD 的规则引擎，问题摘要、字段映射和最终确认仍由 Codex Agent 编排。

## 可行性与缺口

| 目标 | 结论 | 依据/限制 |
| --- | --- | --- |
| 找到名为 Deepworks 的群 | 可行 | `+chat-search` 按群名搜索；零/多候选必须消歧 |
| 找群内指定关键词对话 | 可行 | `+search-msg --chat-query ... --query ...`，支持时间范围和翻页 |
| 读取完整历史 | 有条件可行 | 只能读当前 profile 有权限且搜索服务返回的范围；必须检查分页 ledger |
| 自动生成 TAPD 缺陷 | 可行 | `create_bug(workspace_id, title, options)`；需要 TAPD 凭据和项目字段 |
| 自动生成 TAPD 需求/任务 | 可行 | `create_story_or_task(..., options.entity_type)`；需要区分 `stories`/`tasks` |
| 将钉钉来源带入工单 | 可行但需自行编排 | 工具返回消息 ID/内容，可写入描述；仓库没有现成跨系统 provenance 字段 |
| 无确认持续扫描并避免重复 | 未现成支持 | 需要 DWS 事件监听/调度、消息指纹或 TAPD 自定义字段/外部数据库 |

## 安全与上线前检查

1. **账号和权限**：DWS 需要企业管理员启用 CLI Access；钉钉聊天搜索和 TAPD API 都按实际账号权限生效。
2. **profile 一致性**：群解析、消息读取和写操作应使用同一个 DWS profile；不能跨组织复用 `openConversationId`、`userId` 或 `openDingTalkId`。
3. **结果完整性**：只要 `complete=false`、`hasMore=true` 或存在 `failures`，就把结果标记为 partial，并让用户决定是否缩小时间范围/继续读取。
4. **工单确认**：创建是外部写操作；在 `create_bug`/`create_story_or_task` 前确认 workspace、工单类型、标题和责任人。不要因为模型认为问题明显就跳过确认。
5. **凭据保护**：真实 `TAPD_ACCESS_TOKEN`、API 密码、钉钉 OAuth 数据不得写入本报告或 Git；DWS README 也要求不输出 token、refresh token、appSecret 和 webhook token。
6. **去重设计**：如果以后做定时自动建单，至少持久化 `conversationId + messageId` 或内容哈希，并在 TAPD 写入前查询已有工单，否则重跑会重复创建。
7. **最小验收流程**：先用 DWS `--dry-run`/`--format json` 验证群和消息筛选，再用 TAPD 读取工具验证 workspace/字段，最后用一条明确确认的测试消息创建单；不要用真实群历史直接做批量写入。

## 本次执行结果

- 已通过 npm 安装 DWS CLI `v1.0.61`，可执行文件为 `/opt/homebrew/bin/dws`。
- 已按 `multi` 模式安装 DWS 的产品 Skills，统一根目录为 `~/.agents/skills`，其中包含 `dingtalk-chat`。
- 已验证 `uvx mcp-server-tapd --help` 可运行，并在 `~/.codex/config.toml` 注册 TAPD stdio MCP；配置只从本地 `.env` 读取凭据，不在 TOML 中保存 token 值。
- `dws doctor` 和 `dws auth status` 显示当前尚未登录钉钉；完成 `dws auth login` 后才能读取真实群聊。
- 本次没有执行钉钉登录，也没有创建或修改 TAPD 数据。
