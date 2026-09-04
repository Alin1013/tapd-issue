# Dingtalk TAPD Bridge

这是一个围绕设计文档实现的 Python CLI，用于把钉钉 DWS 中的问题消息整理为 TAPD 缺陷、需求或任务。`search`、`draft`、`create` 仍保持读取和写入分层；`listen` 是用户 @当前用户后触发的受控自动写入入口，并把钉钉 `messageId`、会话 ID 和分页完整性保留在工单中。

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

## @我 自动建单

已完成 DWS 设备登录后，可以启动常驻监听：

```bash
dingtalk-tapd listen
```

监听使用 `dws event +listen-im --kind at-me`，默认只接收已确认的 `DeepWorks 产品交流群`（`cid3SbKZNiotRpk9RdlluSUSA==`）中 @当前用户的消息，并只提交与企业知识中心、知识库或知识管理有关的内容。触发后程序会自动：

- 使用 TAPD 项目 `57379524`，类型固定为 Bug，负责人固定为 `雷艾琳`；
- 根据影响词设置优先级（明确紧急/P0 为 `urgent`，阻断故障为 `high`，建议/咨询为 `low`，无法判断为 `medium`）；
- 生成 `【用户反馈】问题摘要-YYYYMMDD-HHMMSS` 标题；
- 用 `+messages-mget --download-resources` 下载截图/附件，在描述中保留本地路径、资源 ID、消息 ID 和下载失败原因；
- 对本地图片尝试使用系统 `tesseract` 做 OCR。没有中文语言包或视觉分析器时，会明确标注“原图待查看”，不会虚构截图内容；
- 以 `conversationId:messageId` 写入 `.dingtalk-tapd/state.sqlite3`，同一消息只建一次工单。

可用边界参数控制监听生命周期：

```bash
dingtalk-tapd listen --duration 10m
dingtalk-tapd listen --max-events 1
```

高级覆盖项（正常使用不需要填写）：`DINGTALK_TAPD_GROUP_ID`、`DINGTALK_TAPD_GROUP_NAME`、`DINGTALK_TAPD_WORKSPACE_ID`、`DINGTALK_TAPD_OWNER`、`DINGTALK_TAPD_TITLE_PREFIX`、`DINGTALK_TAPD_STATE_DB`、`DINGTALK_TAPD_ATTACHMENT_DIR`、`DINGTALK_TAPD_READY_TIMEOUT`、`DINGTALK_TAPD_OCR_COMMAND`。附件目录必须是工作目录内相对路径，符合 DWS 的下载安全约束。

TAPD MCP 只有在工具契约声明支持时才接受图片/视频公网直链作为富媒体；旧版 MCP 会自动降级为把直链写入描述。钉钉下载到本地的文件不会被冒充成“已上传”，当前未实现把本地文件直接上传到 TAPD 的未验证接口。

该监听默认以当前用户 OAuth 身份运行，不需要创建机器人。若改为企业机器人 Stream，需要额外的开放平台应用、发布审批和入群配置，不能由本项目自动猜测或代办。

只有在明确设置 `TAPD_BACKEND=rest` 时才使用内置 REST 兼容客户端；生产环境建议使用 MCP 后端，以复用设计文档中列出的官方工具契约。

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
- 手动 `create` 没有 `--confirm` 时不会调用 TAPD 创建接口；`listen` 只在目标群收到用户 @当前用户事件后自动写入。
- 令牌只从环境变量读取，不写入 JSON 输出、日志或仓库。
- `listen` 的事件状态和去重键持久化在 `.dingtalk-tapd/state.sqlite3`；写入结果未知时标记为失败供人工排查，不自动重试。
