"""提供历史检索、显式建单、群监听和历史同步自动建单命令。"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
from collections.abc import Callable, Sequence
from queue import Queue
from typing import Any

from .agent import AgentBridgeError, AgentEventForwarder
from .config import AgentConfig, AutomationConfig, DwsConfig, TapdConfig
from .automation import AutoIssueService
from .dws import DwsClient, DwsError
from .models import IssueDraft, IssueType, SearchResult
from .orchestrator import (
    ConfirmationRequired,
    Workflow,
    issue_draft_to_dict,
    search_result_to_dict,
)
from .mcp import McpTapdClient
from .realtime import RealtimeEventError, RealtimeEventListener
from .tapd import TapdClient, TapdError


def _add_search_arguments(parser: argparse.ArgumentParser) -> None:
    """为三个子命令注册一致的 DWS 查询参数。"""

    parser.add_argument("--group", required=True, help="群名称或 openConversationId")
    parser.add_argument("--keyword", required=True, help="消息关键词")
    parser.add_argument("--start", help="ISO 8601 开始时间")
    parser.add_argument("--end", help="ISO 8601 结束时间")
    parser.add_argument("--order", choices=("asc", "desc"), default="asc")


def _build_parser() -> argparse.ArgumentParser:
    """创建参数解析器，并把写入确认设计成显式布尔开关。"""

    parser = argparse.ArgumentParser(description="将钉钉问题消息整理为 TAPD 工单")
    subparsers = parser.add_subparsers(dest="command", required=True)

    search = subparsers.add_parser("search", help="只读检索钉钉消息")
    _add_search_arguments(search)

    draft = subparsers.add_parser("draft", help="只读生成 TAPD 工单草稿")
    _add_search_arguments(draft)
    _add_draft_arguments(draft)

    create = subparsers.add_parser("create", help="确认后创建 TAPD 工单")
    _add_search_arguments(create)
    _add_draft_arguments(create)
    create.add_argument("--confirm", action="store_true", help="确认执行外部写入")
    create.add_argument("--allow-partial", action="store_true", help="允许使用 partial 检索结果建单")

    listen = subparsers.add_parser("listen", help="监听目标群消息并自动创建企业知识中心 TAPD Bug")
    listen.add_argument("--duration", help="监听时长，例如 10m；省略则持续运行")
    listen.add_argument("--max-events", type=int, default=0, help="收到指定数量事件后退出，0 表示不限")

    agent_listen = subparsers.add_parser(
        "agent-listen",
        help="监听目标群消息并转发到远端 GPT/TAPD Bug Agent",
    )
    agent_listen.add_argument("--duration", help="监听时长，例如 10m；省略则持续运行")
    agent_listen.add_argument("--max-events", type=int, default=0, help="收到指定数量事件后退出，0 表示不限")

    sync = subparsers.add_parser("sync", help="同步目标群聊天记录并自动创建相关 TAPD Bug")
    sync.add_argument("--start", help="ISO 8601 开始时间")
    sync.add_argument("--end", help="ISO 8601 结束时间")
    sync.add_argument("--order", choices=("asc", "desc"), default="desc")
    sync.add_argument("--allow-partial", action="store_true", help="允许使用 partial 同步结果自动建单")
    sync.add_argument(
        "--retry-failed",
        action="store_true",
        help="仅重试已知的 TAPD 写入前校验失败事件，不重试未知写入失败",
    )
    return parser


def _add_draft_arguments(parser: argparse.ArgumentParser) -> None:
    """注册 TAPD 草稿参数；实体类型只允许文档约定的三种值。"""

    parser.add_argument("--workspace-id", help="TAPD 项目 ID；不提供时通过 --user-name 消歧")
    parser.add_argument("--user-name", help="TAPD 用户昵称，用于查询参与项目")
    parser.add_argument("--type", dest="issue_type", choices=tuple(issue.value for issue in IssueType), required=True)
    parser.add_argument("--title", help="覆盖自动生成的工单标题")
    parser.add_argument("--owner", help="负责人")
    parser.add_argument("--priority", help="优先级")
    parser.add_argument("--severity", help="严重程度（缺陷）")
    parser.add_argument("--fields-json", help="自定义字段 JSON 对象")


def _parse_fields(value: str | None) -> dict[str, Any]:
    """解析自定义字段并拒绝数组/标量，避免表单字段结构失控。"""

    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("--fields-json 必须是合法 JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("--fields-json 必须是 JSON 对象")
    return parsed


def _workflow() -> Workflow:
    """按环境变量创建客户端；令牌仅由客户端在请求时读取。"""

    tapd_config = TapdConfig.from_env()
    if tapd_config.backend == "mcp":
        tapd = McpTapdClient(tapd_config)
    elif tapd_config.backend == "rest":
        tapd = TapdClient(tapd_config)
    else:
        raise ValueError("TAPD_BACKEND 只能是 mcp 或 rest")
    return Workflow(DwsClient(DwsConfig.from_env()), tapd)


def _prepare(workflow: Workflow, args: argparse.Namespace) -> tuple[SearchResult, IssueDraft]:
    """执行检索和草稿构建，不触发任何 TAPD 写操作。"""

    result = workflow.search(args.group, args.keyword, start=args.start, end=args.end, order=args.order)
    fields = _parse_fields(args.fields_json)
    # 基础字段单独暴露，确保确认预览中能看见负责人/优先级/严重程度。
    for key in ("owner", "priority", "severity"):
        value = getattr(args, key, None)
        if value:
            fields[key] = value
    workspace_id = workflow.resolve_workspace_id(args.workspace_id, args.user_name)
    draft = workflow.build_draft(
        result,
        workspace_id,
        IssueType(args.issue_type),
        args.keyword,
        title=args.title,
        fields=fields,
    )
    return result, draft


def _json_print(value: Any) -> None:
    """统一 JSON 输出格式，便于脚本继续处理并保留中文。"""

    print(json.dumps(value, ensure_ascii=False, indent=2))


def _consume_target_groups(
    automation: AutomationConfig,
    listener_factory: Callable[[str], RealtimeEventListener],
    callback: Callable[[Any], None],
    max_events: int,
) -> None:
    """为每个目标群建立独立 DWS 订阅，并在主线程串行处理事件。"""

    group_ids = tuple(identifier for identifier, _ in automation.configured_groups())
    if not group_ids:
        raise ValueError("至少需要配置一个目标群")
    listeners = [listener_factory(group_id) for group_id in group_ids]
    if len(listeners) == 1:
        listeners[0].consume(callback)
        return

    # DWS 的 --group 只接受一个 openConversationId；并行订阅后用队列汇聚，
    # 让 SQLite 幂等账本和 TAPD 写入仍在当前线程按序执行。
    events: Queue[Any | None] = Queue()
    errors: list[Exception] = []
    workers: list[threading.Thread] = []
    stop_requested = threading.Event()
    for listener in listeners:
        def run(current: RealtimeEventListener = listener) -> None:
            """排空一个群订阅的 stdout，并把异常转交主线程。"""

            try:
                current.consume(events.put)
            except Exception as exc:  # noqa: BLE001 - 主线程统一收束监听异常
                errors.append(exc)
                stop_requested.set()
                for peer in listeners:
                    peer.stop()
            finally:
                events.put(None)

        worker = threading.Thread(target=run, name=f"dws-group-listener-{listener.group_id}", daemon=True)
        workers.append(worker)
        worker.start()

    finished = 0
    processed = 0
    stopping = False
    try:
        while finished < len(listeners):
            item = events.get()
            if item is None:
                finished += 1
                continue
            if stopping or stop_requested.is_set():
                continue
            callback(item)
            processed += 1
            if max_events and processed >= max_events:
                stopping = True
                stop_requested.set()
                for listener in listeners:
                    listener.stop()
    finally:
        for listener in listeners:
            listener.stop()
        for worker in workers:
            worker.join(timeout=2)
    if errors:
        raise errors[0]


def _listen(workflow: Workflow, args: argparse.Namespace) -> int:
    """运行实时自动建单；业务默认值从 AutomationConfig 读取而不是命令行重复填写。"""

    if args.max_events < 0:
        raise ValueError("--max-events 不能为负数")
    automation = AutomationConfig.from_env()
    service = AutoIssueService(workflow, automation)
    def listener_factory(group_id: str) -> RealtimeEventListener:
        """为指定群构造普通自动建单监听器。"""

        return RealtimeEventListener(
            DwsConfig.from_env(),
            automation,
            duration=args.duration,
            max_events=0,
            group_id=group_id,
        )
    try:
        def handle(event: Any) -> None:
            """每条事件单独输出，便于 launchd、日志采集或上层 Agent 增量消费。"""

            _json_print(service.process(event).as_dict())

        _consume_target_groups(automation, listener_factory, handle, args.max_events)
        return 0
    finally:
        service.close()


def _agent_listen(args: argparse.Namespace) -> int:
    """监听 DWS 目标群并把消息/媒体交给远端 Agent，由远端完成卡片和 TAPD 写入。"""

    if args.max_events < 0:
        raise ValueError("--max-events 不能为负数")
    automation = AutomationConfig.from_env()
    agent = AgentConfig.from_env()
    dws = DwsClient(DwsConfig.from_env())
    forwarder = AgentEventForwarder(dws, agent, automation)
    def listener_factory(group_id: str) -> RealtimeEventListener:
        """为指定群构造只接收 @缺陷机器人的远端桥接监听器。"""

        return RealtimeEventListener(
            DwsConfig.from_env(),
            automation,
            duration=args.duration,
            max_events=0,
            mention_targets=automation.bot_mention_targets,
            mention_target_ids=automation.bot_mention_target_ids,
            include_at_me=False,
            group_id=group_id,
        )
    try:
        def handle(event: Any) -> None:
            """每个事件独立转发并输出结果，便于 systemd 日志审计和故障重试。"""

            _json_print(forwarder.process(event))

        _consume_target_groups(automation, listener_factory, handle, args.max_events)
        return 0
    finally:
        # DwsClient 目前由短命 CLI 进程持有，保留兼容未来增加连接池的关闭钩子。
        close = getattr(dws, "close", None)
        if callable(close):
            close()


def _sync(workflow: Workflow, args: argparse.Namespace) -> int:
    """执行一次目标群历史同步；实时监听之外的无 @ 消息也走同一建单规则。"""

    automation = AutomationConfig.from_env()
    service = AutoIssueService(workflow, automation)
    try:
        report = service.sync_history(
            start=args.start,
            end=args.end,
            order=args.order,
            allow_partial=args.allow_partial,
            retry_failed=args.retry_failed,
        )
        _json_print(report.as_dict())
        return 2 if report.blocked_reason else 0
    finally:
        service.close()


def main(argv: Sequence[str] | None = None) -> int:
    """运行 CLI，返回适合 shell 的退出码而不吞掉业务错误。"""

    args = _build_parser().parse_args(argv)
    workflow: Workflow | None = None
    try:
        if args.command == "agent-listen":
            logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
            return _agent_listen(args)
        workflow = _workflow()
        if args.command == "listen":
            logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
            return _listen(workflow, args)
        if args.command == "sync":
            return _sync(workflow, args)
        if args.command == "search":
            result = workflow.search(args.group, args.keyword, start=args.start, end=args.end, order=args.order)
            _json_print(search_result_to_dict(result))
            return 0

        result, draft = _prepare(workflow, args)
        if args.command == "draft":
            _json_print({"search": search_result_to_dict(result), "draft": issue_draft_to_dict(draft)})
            return 0

        if result.is_partial and not args.allow_partial:
            raise ValueError("检索结果为 partial；请缩小范围或显式使用 --allow-partial")
        # 写入前仍先读取项目元数据和自定义字段，让用户确认真实目标与字段定义。
        confirmation = workflow.confirmation_context(draft)
        if not args.confirm:
            _json_print(
                {
                    "confirmationRequired": True,
                    **confirmation,
                    "search": search_result_to_dict(result),
                    "draft": issue_draft_to_dict(draft),
                }
            )
            return 2
        response = workflow.create(draft, confirmed=True)
        _json_print({"created": True, "response": response, "draft": issue_draft_to_dict(draft)})
        return 0
    except (AgentBridgeError, DwsError, TapdError, ConfirmationRequired, RealtimeEventError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    finally:
        if workflow is not None and hasattr(workflow.tapd, "close"):
            workflow.tapd.close()


if __name__ == "__main__":
    raise SystemExit(main())
