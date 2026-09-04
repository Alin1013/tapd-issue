"""消费 DWS 实时群消息事件，并补齐消息详情与本地资源下载。"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import threading
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from queue import Queue
from typing import Any, Callable

from .config import AutomationConfig, DwsConfig
from .dws import DwsClient, DwsError
from .models import DingTalkMessage

logger = logging.getLogger(__name__)

AT_ME_EVENT_TYPE = "user_im_message_receive_at"
GROUP_EVENT_TYPE = "user_im_message_receive_group"
HISTORY_SYNC_EVENT_TYPE = "history-sync"


def _redact_log_line(line: str) -> str:
    """移除可能由外部 CLI 回显的进程环境凭据，避免状态日志泄露密钥。"""

    redacted = line
    for name in ("TAPD_ACCESS_TOKEN", "TAPD_API_USER", "TAPD_API_PASSWORD", "DWS_CLIENT_SECRET"):
        value = os.getenv(name)
        if value:
            redacted = redacted.replace(value, "[REDACTED]")
    return redacted


class RealtimeEventError(DwsError):
    """DWS 实时进程启动、就绪或事件解析失败。"""


@dataclass(frozen=True, slots=True)
class ResourceDownload:
    """记录一项消息资源的下载结果，失败信息也必须进入工单描述。"""

    resource_id: str
    message_id: str
    local_path: str | None = None
    size_bytes: int | None = None
    url: str | None = None
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        """只有存在本地文件且没有错误时才视为下载成功。"""

        return bool(self.local_path) and Path(self.local_path).is_file() and not self.error


@dataclass(frozen=True, slots=True)
class RealtimeEvent:
    """扁平化 DWS IM 消息事件的稳定投影。"""

    message_id: str
    conversation_id: str
    sender_name: str
    content: str
    created_at: str
    resource_refs: tuple[str, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)
    event_type: str = ""

    @classmethod
    def from_message(cls, message: DingTalkMessage) -> "RealtimeEvent":
        """把历史同步得到的消息转换成与实时事件相同的自动建单输入。"""

        # 历史消息没有事件 envelope；显式标记来源，便于描述和排障时区分两条入口。
        return cls(
            message_id=message.message_id,
            conversation_id=message.conversation_id,
            sender_name=message.sender_name,
            content=message.text,
            created_at=message.created_at,
            resource_refs=message.resource_refs,
            event_type=HISTORY_SYNC_EVENT_TYPE,
            raw={"source": "history-sync"},
        )

    def mentions_any(self, names: Sequence[str], identifiers: Sequence[str] = ()) -> bool:
        """判断正文或事件元数据是否 @ 了指定姓名/稳定 ID。"""

        content = self.content or ""
        candidates = tuple(
            dict.fromkeys(
                value.strip().lstrip("@")
                for value in (*names, *identifiers)
                if value.strip().lstrip("@")
            )
        )
        for candidate in candidates:
            # 只接受完整 @ token，避免 @董超杰 被误判成 @董超。
            if re.search(rf"(?<!\S)@{re.escape(candidate)}(?![\u4e00-\u9fffA-Za-z0-9_])", content):
                return True
        for name in names:
            normalized = name.strip().lstrip("@")
            if normalized and re.search(
                rf"<at\b[^>]*>\s*@?{re.escape(normalized)}\s*</at>",
                content,
                flags=re.IGNORECASE,
            ):
                return True
        return _mention_metadata_matches(self.raw, candidates)

    def is_automation_trigger(
        self,
        mention_targets: Sequence[str],
        mention_target_ids: Sequence[str] = (),
    ) -> bool:
        """区分实时三类 @ 触发与历史同步的无 @ 主题扫描。"""

        if self.event_type == HISTORY_SYNC_EVENT_TYPE:
            return True
        # 空 event_type 兼容旧版扁平事件，按原有 @当前用户订阅处理。
        if self.event_type in {"", AT_ME_EVENT_TYPE}:
            return True
        return self.event_type == GROUP_EVENT_TYPE and self.mentions_any(mention_targets, mention_target_ids)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "RealtimeEvent | None":
        """兼容扁平和 payload 包装事件；非消息事件返回 None。"""

        data: Mapping[str, Any] = payload
        nested = payload.get("payload")
        if isinstance(nested, Mapping):
            data = nested
        message_id = _first_text(data, ("message_id", "messageId", "open_message_id", "id"))
        if not message_id:
            return None
        conversation_id = _first_text(
            data,
            ("conversation_id", "conversationId", "open_conversation_id", "openConversationId"),
        )
        if not conversation_id:
            return None
        sender = _first_text(data, ("sender", "sender_name", "senderName", "sender_nick"), "未知发送者")
        content = _first_text(data, ("content", "text", "message_text", "messageText"))
        created_at = _first_text(data, ("create_time", "created_at", "createTime", "event_time", "timestamp"), "未知时间")
        refs = _resource_refs(data)
        event_type = _first_text(payload, ("type", "event_type", "eventType")) or _first_text(
            data, ("type", "event_type", "eventType")
        )
        return cls(
            message_id=message_id,
            conversation_id=conversation_id,
            sender_name=sender,
            content=content,
            created_at=created_at,
            resource_refs=refs,
            event_type=event_type,
            raw=payload,
        )


def _first_text(item: Mapping[str, Any], keys: Sequence[str], default: str = "") -> str:
    """从兼容字段中提取展示文本，避免把对象 repr 写入 TAPD。"""

    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, Mapping):
            nested = value.get("text") or value.get("content") or value.get("name") or value.get("nick")
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    return default


def _resource_refs(item: Mapping[str, Any]) -> tuple[str, ...]:
    """收集事件中的资源 ID/URL，保留顺序并去重。"""

    raw = item.get("resource_refs") or item.get("resourceRefs") or item.get("resources") or ()
    if isinstance(raw, (str, int)):
        raw = (raw,)
    refs: list[str] = []
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        for value in raw:
            if isinstance(value, Mapping):
                value = value.get("url") or value.get("uri") or value.get("resource_id") or value.get("resourceId")
            if value is not None and str(value).strip() and str(value).strip() not in refs:
                refs.append(str(value).strip())
    return tuple(refs)


def _mention_metadata_matches(value: Any, candidates: Sequence[str], in_mention_field: bool = False) -> bool:
    """兼容 DWS 版本把 @对象单独放在 atUsers/mentions 元数据中的返回。"""

    candidate_set = set(candidates)
    if isinstance(value, Mapping):
        for key, nested in value.items():
            field_name = str(key).lower()
            mention_field = in_mention_field or "mention" in field_name or field_name.startswith("at")
            if mention_field and isinstance(nested, (str, int)):
                normalized = str(nested).strip().lstrip("@")
                if normalized in candidate_set or f"@{normalized}" in candidate_set:
                    return True
            if isinstance(nested, (Mapping, list)) and _mention_metadata_matches(nested, candidate_set, mention_field):
                return True
    elif isinstance(value, list):
        return any(_mention_metadata_matches(item, candidate_set, in_mention_field) for item in value)
    return False


class RealtimeEventListener:
    """托管 DWS 长连接生命周期，并在 ready 后逐行产出消息事件。"""

    def __init__(
        self,
        config: DwsConfig,
        automation: AutomationConfig,
        *,
        duration: str | None = None,
        max_events: int = 0,
    ) -> None:
        self.config = config
        self.automation = automation
        self.duration = duration
        self.max_events = max_events
        self._process: subprocess.Popen[str] | None = None
        self._ready = threading.Event()
        self._stderr_done = threading.Event()
        self._stderr_thread: threading.Thread | None = None
        self._stop_requested = False

    def _command(self) -> list[str]:
        """构造同时订阅 @当前用户和目标群事件的 NDJSON 监听命令。"""

        args = [
            self.config.executable,
            "event",
            "consume",
            AT_ME_EVENT_TYPE,
            GROUP_EVENT_TYPE,
            "--group",
            self.automation.group_id,
            "--flatten",
            "--format",
            "ndjson",
        ]
        if self.duration:
            args.extend(["--duration", self.duration])
        # max-events 在本地按过滤后的唯一消息计数，避免群内无关消息耗尽预算。
        if self.config.profile:
            args.extend(["--profile", self.config.profile])
        return args

    def _drain_stderr(self) -> None:
        """持续消费状态流，避免长连接因 stderr 管道背压而停止。"""

        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            for line in process.stderr:
                stripped = line.strip()
                if "[event] ready " in stripped or "[event] ready" in stripped:
                    self._ready.set()
                if stripped:
                    logger.info("DWS: %s", _redact_log_line(stripped))
        finally:
            self._stderr_done.set()

    def _start(self) -> None:
        """启动并等待 ready marker，防止在订阅尚未建立时误读 stdout。"""

        if self._process is not None:
            return
        env = os.environ.copy()
        try:
            self._process = subprocess.Popen(
                self._command(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env,
            )
        except OSError as exc:
            raise RealtimeEventError(f"无法启动 DWS CLI: {self.config.executable}") from exc
        self._stderr_thread = threading.Thread(target=self._drain_stderr, name="dws-stderr", daemon=True)
        self._stderr_thread.start()
        if not self._ready.wait(self.automation.ready_timeout_seconds):
            return_code = self._process.poll()
            self.stop()
            raise RealtimeEventError(f"DWS 实时监听未在 {self.automation.ready_timeout_seconds:g}s 内就绪 (code={return_code})")
        if self._process.poll() is not None:
            raise RealtimeEventError("DWS 实时监听在 ready 后意外退出")

    def events(self) -> Iterator[RealtimeEvent]:
        """消费 NDJSON；单行格式错误只跳过该行，避免中断后续事件。"""

        self._start()
        try:
            yield from self._read_events()
        finally:
            self.stop()

    def consume(self, callback: Callable[[RealtimeEvent], None]) -> None:
        """在独立读取线程中持续排空 stdout，再由调用方顺序处理事件。"""

        self._start()
        queue: Queue[RealtimeEvent | None] = Queue()
        errors: list[Exception] = []

        def reader() -> None:
            """把事件快速放入无界队列，避免 TAPD 网络请求造成 DWS 管道背压。"""

            try:
                for event in self._read_events():
                    queue.put(event)
            except Exception as exc:  # noqa: BLE001 - 主线程会重新抛出读取异常
                errors.append(exc)
            finally:
                queue.put(None)

        reader_thread = threading.Thread(target=reader, name="dws-stdout", daemon=True)
        reader_thread.start()
        try:
            while True:
                event = queue.get()
                if event is None:
                    if errors:
                        raise RealtimeEventError("DWS 实时事件读取失败") from errors[0]
                    return
                callback(event)
        finally:
            self.stop()
            reader_thread.join(timeout=1)

    def _read_events(self) -> Iterator[RealtimeEvent]:
        """读取并筛选事件；群订阅只放行 @ 指定对象的消息。"""

        process = self._process
        if process is None or process.stdout is None:
            raise RealtimeEventError("DWS 实时监听没有 stdout")
        accepted = 0
        seen: set[tuple[str, str]] = set()
        for line in process.stdout:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                logger.warning("忽略无法解析的 DWS 事件行")
                continue
            if not isinstance(payload, Mapping):
                continue
            event = RealtimeEvent.from_payload(payload)
            if event is None or not event.is_automation_trigger(
                self.automation.mention_targets,
                self.automation.mention_target_ids,
            ):
                continue
            event_key = (event.conversation_id, event.message_id)
            if event_key in seen:
                continue
            seen.add(event_key)
            yield event
            accepted += 1
            if self.max_events and accepted >= self.max_events:
                return

    def stop(self) -> None:
        """优先用 stdin EOF/SIGTERM 触发 DWS 清理，不使用 SIGKILL 跳过订阅回收。"""

        if self._stop_requested:
            return
        self._stop_requested = True
        process, self._process = self._process, None
        if process is None:
            return
        if process.poll() is None:
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logger.error("DWS 未在退出窗口内结束，保留子进程以便宿主继续清理")
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=1)

    def __enter__(self) -> "RealtimeEventListener":
        """支持 with 语法，确保异常路径也取消订阅。"""

        self._start()
        return self

    def __exit__(self, *_: object) -> None:
        """离开上下文时释放 DWS 进程。"""

        self.stop()


def extract_downloads(payload: Any, message_id: str) -> tuple[ResourceDownload, ...]:
    """从 messages-mget 返回中提取下载 ledger，不假设固定嵌套层级。"""

    candidates: list[Mapping[str, Any]] = []

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            if any(key in value for key in ("localPath", "local_path", "resourceId", "resource_id")):
                candidates.append(value)
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(payload)
    downloads: list[ResourceDownload] = []
    seen: set[tuple[str, str | None]] = set()
    for item in candidates:
        resource_id = _first_text(item, ("resourceId", "resource_id", "fileId", "mediaId", "id"), "未知资源")
        local_path = _first_text(item, ("localPath", "local_path", "path")) or None
        url = _first_text(item, ("url", "uri")) or None
        error = _first_text(item, ("error", "failure", "message")) or None
        size_value = item.get("sizeBytes") or item.get("size_bytes")
        try:
            size_bytes = int(size_value) if size_value is not None else None
        except (TypeError, ValueError):
            size_bytes = None
        key = (resource_id, local_path)
        if key in seen:
            continue
        seen.add(key)
        downloads.append(ResourceDownload(resource_id, message_id, local_path, size_bytes, url, error))
    return tuple(downloads)


def fetch_message_details(
    dws: DwsClient,
    event: RealtimeEvent,
    automation: AutomationConfig,
) -> tuple[Any, tuple[ResourceDownload, ...]]:
    """按真实消息 ID 批量读取详情并下载资源，保留完整返回供分析器使用。"""

    output_dir = Path(automation.attachment_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = dws.get_messages(
        (event.message_id,),
        download_resources=True,
        output_dir=str(output_dir),
    )
    return payload, extract_downloads(payload, event.message_id)
