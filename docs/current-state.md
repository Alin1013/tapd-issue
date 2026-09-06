# 当前内容总览

本文以仓库当前实现为准，记录截至 2026-09-06 已经具备的能力、业务默认口径和运行边界。安装与逐步操作见 [操作手册](operations.md)，环境变量与权限见 [配置说明](configuration.md)。

## 一句话定位

这是一个把钉钉 DWS 群消息整理为 TAPD 工单的桥接项目：Python Bridge 负责读取消息、筛选主题、追踪来源和本地幂等；Node Bug Agent 负责钉钉机器人回调、媒体分析、责任人校验和 TAPD Bug 写入。

## 当前内容

| 内容 | 当前实现 | 主要入口 |
| --- | --- | --- |
| 手工检索 | 按群、关键词、时间和顺序读取消息，输出消息 ID、会话 ID 和分页完整性 | `dingtalk-tapd search` |
| 手工草稿 | 读取 TAPD 项目/字段后生成草稿，不调用写接口 | `dingtalk-tapd draft` |
| 手工建单 | 先预览，只有显式 `--confirm` 才创建 Bug、需求或任务 | `dingtalk-tapd create` |
| 实时自动建单 | 订阅目标群全部消息，按企业知识中心主题筛选后自动创建 Bug | `dingtalk-tapd listen` |
| 历史补扫 | 扫描目标群历史消息；无 @ 也能按正文/OCR 主题建单 | `dingtalk-tapd sync` |
| 远端 Agent 桥接 | 只接收目标群中 @缺陷机器人的事件，下载媒体后以 HMAC 签名转发 | `dingtalk-tapd agent-listen` |
| Node H5 | 提供动态字段、媒体上传和手工创建表单 | `GET /` |
| Node 互动卡片 | 在人工确认模式下编辑字段并在钉钉窗口内确认/取消 | `/dingtalk/card-callback` |

## 两条运行链路

### 本地自动建单

```text
DWS 目标群事件
  -> Python 读取消息和附件
  -> 主题筛选（企业知识中心/知识库/知识管理）
  -> OCR 与优先级判断
  -> TAPD 只读校验（workspace、字段、成员）
  -> 创建 Bug + 保存来源和状态
```

`listen` 读取目标群的全部事件，不要求 @；`sync` 读取历史消息，也不要求 @。两者都只对命中主题的消息建单，并使用 `conversationId:messageId` 作为事件键。

### 钉钉机器人与远端 Bug Agent

```text
钉钉文本/截图/视频
  -> Node 回调验签与媒体下载
  -> TAPD 候选字段读取
  -> 模型生成 JSON 草稿
  -> 责任白名单与项目成员校验
  -> 自动创建 Bug（默认）或卡片/H5 人工确认
  -> 结果回钉钉会话或目标群
```

`agent-listen` 是 Python 到 Node 的桥接入口，只放行 @缺陷机器人；`/api/agent/events` 收到合法事件后立即返回 `202 accepted`，分析和建单在后台进行。事件重复时返回 `duplicate`，避免桥接重试重复建单。

## 当前业务默认口径

| 项目 | 默认值/规则 |
| --- | --- |
| TAPD 项目 | `57379524` |
| 自动工单类型 | Bug |
| 标题前缀 | `【企业知识中心—用户反馈】` |
| 主题关键词 | `企业知识中心`、`企业知识`、`知识中心`、`知识库`、`知识管理` |
| 优先级 | 紧急/P0/线上全量/数据丢失/完全不可用 → `urgent`；失败/报错/异常/无法/卡死等 → `high`；建议/优化/咨询等 → `low`；其他 → `medium` |
| 默认负责人 | `雷艾琳`；命中模块责任规则时由规则覆盖 |
| 默认开发人 | 空；命中模块责任规则时由规则覆盖 |
| 默认测试人 | `雷艾琳`，当前自动流程不按模块切换 |
| 模块责任规则 | 编译 → 杨耀发；抽取 → 肖文杨；本体 → 肖文杨；对话/对话部分 → 杨耀发 |
| 默认字段 | 迭代 `企业知识中心9月`、发现版本 `v1.3.0`、模块 `企业知识中心`、测试方式 `手工测试`、迭代需求缺陷 `是`；只有命中 TAPD 候选时才写入 |

显式设置责任白名单后，会完整覆盖内置模块规则；Python 使用 `DINGTALK_TAPD_RESPONSIBILITY_WHITELIST`，Node 使用 `TAPD_RESPONSIBILITY_WHITELIST`，两端不会自动同步配置。

## 来源、媒体和状态

- 每个自动事件都保留群名、`conversationId`、`messageId`、发送者和时间；描述中还会保留资源 ID、下载路径或失败原因。
- Python 默认调用本机 `tesseract` 的 `eng` 语言包；可用 `DINGTALK_TAPD_OCR_COMMAND` 提供自定义命令。OCR 或下载失败不会虚构图片内容，会保留原图/告警供人工查看。
- Node 最多接收 5 个媒体，单个不超过 8 MB，总计不超过 12 MB；视频最多抽取 4 帧给模型，原视频仍保留在媒体目录。
- TAPD Bug 创建成功后再逐个上传附件；附件失败不会回滚 Bug，响应、日志或卡片会列出失败项。
- Python 状态写入 `.dingtalk-tapd/state.sqlite3`：`created`、`ignored`、`duplicate`、`failed`。未知写入结果不会自动重试；`--retry-failed` 只补偿已知的写入前校验失败。
- Node 草稿、表单会话、回调去重和桥接事件去重均为进程内状态：草稿默认 30 分钟，表单状态 15 分钟，桥接事件去重 30 分钟；服务重启会丢失未确认草稿。

## 当前已知边界

1. DWS 和 TAPD 仍严格受当前账号、profile、workspace 和字段权限限制，系统不会绕过钉钉可见范围。
2. `listen`/`sync` 的自动写入没有人工确认门禁；生产启用前应先用单条测试消息和 `draft` 验证目标群、workspace 与责任人。
3. `TAPD_AUTO_CREATE_BUGS=false` 才会进入 Node 草稿确认模式；卡片模板缺失或投递失败时回退到 H5 草稿链接。
4. 媒体 URL 当前主要用于内网或 PoC；生产部署应提供公网 HTTPS、访问控制和媒体清理策略。
5. Node 的草稿和去重状态尚未持久化到 Redis/数据库；需要跨重启可靠恢复时必须补充外部状态存储。

## 推荐阅读顺序

1. 先看 [当前内容总览](current-state.md)，确认使用哪条链路。
2. 再按 [操作手册](operations.md) 完成启动、验证、建单和运维。
3. 修改部署或环境变量时查 [配置说明](configuration.md)。
4. 需要理解 DWS/TAPD 原始契约时再看 [集成研究](dingtalk-tapd-research.md)。
