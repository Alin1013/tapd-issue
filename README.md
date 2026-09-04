# Dingtalk TAPD Bridge

这是一个围绕设计文档实现的 Python CLI，用于把钉钉 DWS 中的问题消息整理为 TAPD 缺陷、需求或任务。项目刻意将读取和写入分成不同命令，并把钉钉 `messageId`、会话 ID 和分页完整性保留在草稿中。

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
- 没有 `--confirm` 时不会调用任何 TAPD 创建接口。
- 令牌只从环境变量读取，不写入 JSON 输出、日志或仓库。
- 自动持续扫描和去重存储不在本项目范围内；草稿中的来源 ID 可作为后续幂等键。
