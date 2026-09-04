"""调用 DWS CLI 完成群组解析和消息检索，并校验结果完整性。"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .config import DwsConfig
from .models import DingTalkGroup, DingTalkMessage, PaginationLedger, SearchResult


class DwsError(RuntimeError):
    """DWS 调用或响应不符合预期时抛出的基础异常。"""


class DwsCommandError(DwsError):
    """DWS CLI 返回非零退出码时携带可读错误信息。"""


class GroupResolutionError(DwsError):
    """群名无法唯一解析时阻止后续消息读取。"""

    def __init__(self, query: str, candidates: Sequence[DingTalkGroup]) -> None:
        self.query = query
        self.candidates = tuple(candidates)
        if not candidates:
            message = f"没有找到群: {query}"
        else:
            names = ", ".join(group.name for group in candidates)
            message = f"群名存在多个候选，请先消歧: {names}"
        super().__init__(message)


@dataclass(slots=True)
class DwsClient:
    """DWS JSON 客户端；只暴露安全的只读聊天操作。"""

    config: DwsConfig
    timeout_seconds: float = 30.0

    def _run_json(self, command_args: Sequence[str]) -> Any:
        """执行一个 DWS JSON 命令，保留 stderr 但不打印敏感环境变量。"""

        args = [self.config.executable, "chat", *command_args, "--format", "json"]
        if self.config.profile:
            args.extend(["--profile", self.config.profile])
        try:
            completed = subprocess.run(
                args,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except OSError as exc:
            raise DwsCommandError(f"无法执行 DWS CLI: {self.config.executable}") from exc
        except subprocess.TimeoutExpired as exc:
            raise DwsCommandError("DWS CLI 请求超时") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip() or "无错误详情"
            raise DwsCommandError(f"DWS CLI 失败({completed.returncode}): {detail}")
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise DwsCommandError("DWS CLI 返回的不是合法 JSON") from exc

    @staticmethod
    def _items(payload: Any, keys: Sequence[str]) -> list[Mapping[str, Any]]:
        """兼容 DWS 不同版本的列表包装字段，同时拒绝非对象条目。"""

        current = payload
        raw_items: list[Any] | None = None
        # 部分版本会返回 {data: {messages: [...]}}，最多展开两层以防异常递归。
        for _ in range(3):
            if isinstance(current, list):
                raw_items = current
                break
            if not isinstance(current, Mapping):
                break
            for key in keys:
                value = current.get(key)
                if isinstance(value, list):
                    raw_items = value
                    break
            if raw_items is not None:
                break
            nested = current.get("data")
            if isinstance(nested, Mapping):
                current = nested
                continue
            raw_items = [current]
            break
        if raw_items is None:
            raise DwsError("DWS 返回的数据结构不是对象或数组")
        return [item for item in raw_items if isinstance(item, Mapping)]

    @staticmethod
    def _first_text(item: Mapping[str, Any], keys: Sequence[str], default: str = "") -> str:
        """从多个兼容字段中提取字符串，避免把展示对象直接写入工单。"""

        for key in keys:
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, Mapping):
                nested = value.get("name") or value.get("text") or value.get("content")
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
        return default

    def search_groups(self, query: str) -> tuple[DingTalkGroup, ...]:
        """按群名查询候选，不在客户端擅自选择第一项。"""

        if not query.strip():
            raise ValueError("群名查询不能为空")
        payload = self._run_json(["+chat-search", "--query", query, "--page-all"])
        groups: list[DingTalkGroup] = []
        # DWS 群搜索的稳定投影使用 chats 包装；保留旧字段兼容不同 CLI 版本。
        for item in self._items(payload, ("groups", "chats", "items", "data", "results")):
            name = self._first_text(item, ("name", "title", "groupName", "conversationName"))
            conversation_id = self._first_text(
                item,
                ("openConversationId", "open_conversation_id", "conversationId", "conversation_id"),
            )
            if name and conversation_id:
                groups.append(DingTalkGroup(name, conversation_id))
        return tuple(groups)

    def resolve_group(self, query: str) -> DingTalkGroup:
        """要求群名精确对应一个会话，零命中或多候选均抛出异常。"""

        candidates = self.search_groups(query)
        if len(candidates) != 1:
            raise GroupResolutionError(query, candidates)
        return candidates[0]

    def search_messages(
        self,
        group: DingTalkGroup,
        query: str,
        *,
        start: str | None = None,
        end: str | None = None,
        order: str = "asc",
    ) -> SearchResult:
        """在已解析会话中按关键词和时间范围读取消息，并返回完整性账本。"""

        if not query.strip():
            raise ValueError("消息关键词不能为空")
        if order not in {"asc", "desc"}:
            raise ValueError("order 只能是 asc 或 desc")
        args = [
            "+search-msg",
            "--chat-query",
            group.open_conversation_id,
            "--query",
            query,
            "--order",
            order,
            "--page-all",
        ]
        if start:
            args.extend(["--start", start])
        if end:
            args.extend(["--end", end])
        payload = self._run_json(args)
        messages = tuple(self._parse_message(item, group) for item in self._items(payload, ("messages", "items", "data", "results")))
        ledger = self._parse_ledger(payload)
        return SearchResult(group=group, messages=messages, ledger=ledger)

    @classmethod
    def _parse_message(cls, item: Mapping[str, Any], group: DingTalkGroup) -> DingTalkMessage:
        """将 DWS 消息对象归一化，缺失消息 ID 时拒绝建立不可追溯草稿。"""

        message_id = cls._first_text(item, ("messageId", "message_id", "id"))
        conversation_id = cls._first_text(
            item,
            ("conversationId", "conversation_id", "openConversationId"),
            group.open_conversation_id,
        )
        sender_name = cls._first_text(item, ("senderName", "sender", "senderNick", "author"), "未知发送者")
        text = cls._first_text(item, ("text", "content", "body", "messageText"), "")
        created_at = cls._first_text(item, ("createdAt", "created_at", "timestamp", "time"))
        refs = item.get("resourceRefs") or item.get("resource_refs") or ()
        if isinstance(refs, str):
            refs = (refs,)
        if isinstance(refs, Sequence) and not isinstance(refs, (str, bytes)):
            # 资源引用可能是 URL 字符串，也可能是带 uri/id 的对象；两者都不能丢失。
            resource_refs = tuple(
                ref
                if isinstance(ref, str)
                else cls._first_text(ref, ("url", "uri", "resourceRef", "id"), json.dumps(ref, ensure_ascii=False))
                for ref in refs
                if isinstance(ref, (str, Mapping))
            )
        else:
            resource_refs = ()
        return DingTalkMessage(message_id, conversation_id, sender_name, text, created_at, resource_refs)

    @staticmethod
    def _parse_ledger(payload: Any) -> PaginationLedger:
        """解析分页字段；缺少 complete 时默认不完整，遵循保守安全策略。"""

        if not isinstance(payload, Mapping):
            return PaginationLedger(complete=False, stop_reason="invalid-payload")
        metadata: dict[str, Any] = dict(payload)
        # 分页元数据有时位于顶层、pagination/meta 或 data.pagination 中。
        containers: list[Any] = [payload.get("pagination"), payload.get("meta")]
        data = payload.get("data")
        if isinstance(data, Mapping):
            containers.extend([data, data.get("pagination"), data.get("meta")])
        for container in containers:
            if isinstance(container, Mapping):
                metadata.update(container)
        failures_value = metadata.get("failures") or ()
        if isinstance(failures_value, Sequence) and not isinstance(failures_value, str):
            failures = tuple(
                item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
                for item in failures_value
            )
        else:
            failures = (str(failures_value),) if failures_value else ()
        next_page = metadata.get("nextPage") or metadata.get("next_page")
        if isinstance(next_page, str) and next_page.isdigit():
            next_page = int(next_page)
        return PaginationLedger(
            complete=metadata.get("complete") is True,
            has_more=metadata.get("hasMore") is True or metadata.get("has_more") is True,
            next_cursor=metadata.get("nextCursor") or metadata.get("next_cursor"),
            next_page=int(next_page) if isinstance(next_page, int) else None,
            stop_reason=metadata.get("stopReason") or metadata.get("stop_reason"),
            failures=failures,
        )
