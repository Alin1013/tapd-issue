"""编排 DWS 检索、问题草稿生成和确认后的 TAPD 写入。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .dws import DwsClient
from .models import (
    DingTalkMessage,
    IssueDraft,
    IssueType,
    SearchResult,
    SourceReference,
)


class TapdGateway(Protocol):
    """限定编排层所需的 TAPD 能力，使 MCP 与 REST 后端可互换。"""

    def get_user_participant_projects(self, user_name: str) -> Any:
        """返回指定用户参与的项目。"""

        ...

    def get_workspace_info(self, workspace_id: str) -> Any:
        """返回项目元数据。"""

        ...

    def get_entity_custom_fields(self, workspace_id: str, entity_type: IssueType) -> Any:
        """返回指定实体的字段定义。"""

        ...

    def create_bug(self, draft: IssueDraft) -> Any:
        """创建缺陷。"""

        ...

    def create_story_or_task(self, draft: IssueDraft) -> Any:
        """创建需求或任务。"""

        ...


class ConfirmationRequired(RuntimeError):
    """未获得明确确认时阻止任何 TAPD 写操作。"""


class WorkspaceResolutionError(ValueError):
    """TAPD workspace 无法从用户参与项目列表唯一确定时抛出。"""


@dataclass(slots=True)
class Workflow:
    """执行一次从消息检索到工单写入的有界工作流。"""

    dws: DwsClient
    tapd: TapdGateway

    def resolve_workspace_id(self, workspace_id: str | None, user_name: str | None) -> str:
        """优先使用显式 workspace，否则按昵称唯一解析参与项目。"""

        if workspace_id and workspace_id.strip():
            return workspace_id.strip()
        if not user_name or not user_name.strip():
            raise WorkspaceResolutionError("请提供 --workspace-id，或提供 --user-name 用于项目消歧")
        payload = self.tapd.get_user_participant_projects(user_name)
        candidates = self._workspace_candidates(payload)
        if len(candidates) != 1:
            names = ", ".join(f"{item['id']}({item['name']})" for item in candidates)
            detail = names or "无项目"
            raise WorkspaceResolutionError(f"用户参与项目无法唯一确定: {detail}")
        return candidates[0]["id"]

    @staticmethod
    def _workspace_candidates(payload: Any) -> tuple[dict[str, str], ...]:
        """兼容 TAPD MCP/REST 的项目列表包装，并丢弃无 ID 条目。"""

        raw: Any = payload
        current: Any = payload
        for _ in range(3):
            if not isinstance(current, Mapping):
                break
            found = False
            for key in ("workspaces", "projects", "items", "data", "results"):
                value = current.get(key)
                if isinstance(value, list):
                    raw = value
                    found = True
                    break
                if key == "data" and isinstance(value, Mapping):
                    current = value
                    found = True
                    break
            if not found or isinstance(raw, list):
                break
        if not isinstance(raw, list):
            raw = [raw] if isinstance(raw, Mapping) else []
        candidates: list[dict[str, str]] = []
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            identifier = item.get("workspace_id") or item.get("workspaceId") or item.get("id")
            name = item.get("name") or item.get("workspace_name") or item.get("title") or "未命名"
            if identifier is not None and str(identifier).strip():
                candidates.append({"id": str(identifier), "name": str(name)})
        return tuple(candidates)

    @staticmethod
    def validate_custom_fields(definition: Any, fields: Mapping[str, Any]) -> None:
        """依据已读取的字段配置拒绝未知自定义字段，避免把别名猜测传给 TAPD。"""

        basic = {"owner", "priority", "severity", "description", "title", "name"}
        requested = {str(key) for key in fields if str(key) not in basic}
        if not requested:
            return
        allowed: set[str] = set()
        pending: list[Any] = [definition]
        while pending:
            item = pending.pop()
            if isinstance(item, list):
                pending.extend(item)
                continue
            if not isinstance(item, Mapping):
                continue
            for key in ("name", "field_name", "fieldName", "alias", "key", "id"):
                value = item.get(key)
                if value is not None and str(value).strip():
                    allowed.add(str(value))
            for key in ("fields", "custom_fields", "customFields", "items", "data", "results"):
                nested = item.get(key)
                if isinstance(nested, (list, Mapping)):
                    pending.append(nested)
        unknown = sorted(requested - allowed)
        if unknown:
            raise ValueError(f"TAPD 自定义字段未在配置中找到: {', '.join(unknown)}")

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
                resource_refs=message.resource_refs,
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

    def confirmation_context(self, draft: IssueDraft) -> dict[str, Any]:
        """集中读取项目/字段元数据并完成字段校验，供 CLI 展示确认。"""

        workspace = self.tapd.get_workspace_info(draft.workspace_id)
        custom_fields = self.tapd.get_entity_custom_fields(draft.workspace_id, draft.issue_type)
        self.validate_custom_fields(custom_fields, draft.fields)
        return {"workspace": workspace, "customFields": custom_fields}

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
            + (f" 资源：{', '.join(message.resource_refs)}" if message.resource_refs else "")
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
