"""定义跨钉钉与 TAPD 的不可变领域对象，隔离外部 JSON 字段差异。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping


class IssueType(StrEnum):
    """TAPD 支持的工单实体类型。"""

    BUG = "bug"
    STORY = "stories"
    TASK = "tasks"


@dataclass(frozen=True, slots=True)
class DingTalkGroup:
    """表示 DWS 已解析出的唯一群会话。"""

    name: str
    open_conversation_id: str

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("群名称不能为空")
        if not self.open_conversation_id.strip():
            raise ValueError("openConversationId 不能为空")


@dataclass(frozen=True, slots=True)
class DingTalkMessage:
    """保存可追溯的钉钉消息摘要，而不是直接暴露原始响应。"""

    message_id: str
    conversation_id: str
    sender_name: str
    text: str
    created_at: str
    resource_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        required = {
            "message_id": self.message_id,
            "conversation_id": self.conversation_id,
            "created_at": self.created_at,
        }
        missing = [name for name, value in required.items() if not value.strip()]
        if missing:
            raise ValueError(f"消息缺少必填字段: {', '.join(missing)}")


@dataclass(frozen=True, slots=True)
class PaginationLedger:
    """记录 DWS 分页结果，避免将不完整历史误报为完整结果。"""

    complete: bool
    has_more: bool = False
    next_cursor: str | None = None
    next_page: int | str | None = None
    stop_reason: str | None = None
    failures: tuple[str, ...] = ()

    @property
    def is_complete(self) -> bool:
        """只有明确完成且没有后续页、游标或失败，结果才可用于完整性声明。"""

        return (
            self.complete
            and not self.has_more
            and not self.next_cursor
            and self.next_page is None
            and not self.failures
        )


@dataclass(frozen=True, slots=True)
class SearchResult:
    """封装群、消息和分页账本，供摘要与确认流程共同使用。"""

    group: DingTalkGroup
    messages: tuple[DingTalkMessage, ...]
    ledger: PaginationLedger

    @property
    def is_partial(self) -> bool:
        """向用户标记任何无法证明完整的检索结果。"""

        return not self.ledger.is_complete


@dataclass(frozen=True, slots=True)
class SourceReference:
    """写入 TAPD 描述的来源引用，保留消息级去重线索。"""

    conversation_id: str
    message_id: str
    sender_name: str
    created_at: str
    resource_refs: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, str]:
        """输出稳定字段名，便于序列化到描述或外部存储。"""

        return {
            "conversationId": self.conversation_id,
            "messageId": self.message_id,
            "sender": self.sender_name,
            "createdAt": self.created_at,
            "resourceRefs": list(self.resource_refs),
        }


@dataclass(frozen=True, slots=True)
class IssueDraft:
    """表示尚未写入 TAPD 的问题草稿，写入动作必须经过确认门禁。"""

    issue_type: IssueType
    workspace_id: str
    title: str
    description: str
    sources: tuple[SourceReference, ...] = ()
    fields: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.workspace_id.strip():
            raise ValueError("workspace_id 不能为空")
        if not self.title.strip():
            raise ValueError("工单标题不能为空")
        if not self.description.strip():
            raise ValueError("工单描述不能为空")
