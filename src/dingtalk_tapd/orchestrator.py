"""编排 DWS 检索、问题草稿生成和确认后的 TAPD 写入。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .dws import DwsClient
from .models import (
    DingTalkMessage,
    IssueDraft,
    IssueType,
    SearchResult,
    SourceReference,
)
from .tapd import TapdClient


class ConfirmationRequired(RuntimeError):
    """未获得明确确认时阻止任何 TAPD 写操作。"""


@dataclass(slots=True)
class Workflow:
    """执行一次从消息检索到工单写入的有界工作流。"""

    dws: DwsClient
    tapd: TapdClient

    def search(
        self,
        group_query: str,
        keyword: str,
        *,
        start: str | None = None,
        end: str | None = None,
        order: str = "asc",
    ) -> SearchResult:
        """先唯一解析群，再在同一会话中检索消息，避免跨 profile 串数据。"""

        group = self.dws.resolve_group(group_query)
        return self.dws.search_messages(group, keyword, start=start, end=end, order=order)

    @staticmethod
    def build_draft(
        result: SearchResult,
        workspace_id: str,
        issue_type: IssueType,
        keyword: str,
        *,
        title: str | None = None,
        fields: Mapping[str, Any] | None = None,
    ) -> IssueDraft:
        """将消息去重并拼成待确认草稿；来源 ID 始终写入描述。"""

        unique_messages = Workflow._deduplicate_messages(result.messages)
        if not unique_messages:
            raise ValueError("没有可用于建单的消息")
        final_title = title.strip() if title and title.strip() else f"{keyword.strip()}（钉钉问题）"
        description = Workflow._description(result, unique_messages)
        sources = tuple(
            SourceReference(
                conversation_id=message.conversation_id,
                message_id=message.message_id,
                sender_name=message.sender_name,
                created_at=message.created_at,
            )
            for message in unique_messages
        )
        return IssueDraft(
            issue_type=issue_type,
            workspace_id=workspace_id,
            title=final_title,
            description=description,
            sources=sources,
            fields=dict(fields or {}),
        )

    def create(self, draft: IssueDraft, *, confirmed: bool) -> Any:
        """只有 confirmed 为真时才调用 TAPD 写工具，形成不可绕过的门禁。"""

        if not confirmed:
            raise ConfirmationRequired("创建 TAPD 工单前必须显式确认")
        if draft.issue_type is IssueType.BUG:
            return self.tapd.create_bug(draft)
        return self.tapd.create_story_or_task(draft)

    @staticmethod
    def _deduplicate_messages(messages: tuple[DingTalkMessage, ...]) -> tuple[DingTalkMessage, ...]:
        """按会话和消息 ID 去重，保留 DWS 返回的时间顺序。"""

        seen: set[tuple[str, str]] = set()
        unique: list[DingTalkMessage] = []
        for message in messages:
            key = (message.conversation_id, message.message_id)
            if key in seen:
                continue
            seen.add(key)
            unique.append(message)
        return tuple(unique)

    @staticmethod
    def _description(result: SearchResult, messages: tuple[DingTalkMessage, ...]) -> str:
        """生成可读 Markdown，同时明确标记检索是否 partial。"""

        integrity = "完整" if not result.is_partial else "部分结果（请核对时间范围或分页失败）"
        lines = [
            "## 问题摘要",
            "",
            "\n\n".join(message.text or "（消息没有可提取文本）" for message in messages),
            "",
            f"## 来源（{integrity}）",
        ]
        lines.extend(
            f"- {message.created_at} {message.sender_name}：`{message.message_id}`"
            for message in messages
        )
        if result.ledger.stop_reason:
            lines.append(f"- 检索停止原因：{result.ledger.stop_reason}")
        if result.ledger.failures:
            lines.extend(f"- 分页失败：{failure}" for failure in result.ledger.failures)
        return "\n".join(lines)


def search_result_to_dict(result: SearchResult) -> dict[str, Any]:
    """将只读检索结果序列化为 CLI/API 可消费的 JSON。"""

    return {
        "group": {
            "name": result.group.name,
            "openConversationId": result.group.open_conversation_id,
        },
        "messages": [
            {
                "messageId": message.message_id,
                "conversationId": message.conversation_id,
                "sender": message.sender_name,
                "text": message.text,
                "createdAt": message.created_at,
                "resourceRefs": list(message.resource_refs),
            }
            for message in result.messages
        ],
        "integrity": {
            "complete": result.ledger.complete,
            "hasMore": result.ledger.has_more,
            "nextCursor": result.ledger.next_cursor,
            "nextPage": result.ledger.next_page,
            "stopReason": result.ledger.stop_reason,
            "failures": list(result.ledger.failures),
            "isPartial": result.is_partial,
        },
    }


def issue_draft_to_dict(draft: IssueDraft) -> dict[str, Any]:
    """将草稿序列化为确认提示，敏感配置不在其中出现。"""

    return {
        "issueType": draft.issue_type.value,
        "workspaceId": draft.workspace_id,
        "title": draft.title,
        "description": draft.description,
        "sources": [source.as_dict() for source in draft.sources],
        "fields": dict(draft.fields),
    }
