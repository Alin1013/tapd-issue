"""钉钉 DWS 到 TAPD 的领域模型、实时监听与编排组件。"""

from .automation import AutoIssueService, AutomationOutcome, IssueAnalysis
from .config import AutomationConfig

from .models import (
    DingTalkGroup,
    DingTalkMessage,
    IssueDraft,
    IssueType,
    PaginationLedger,
    SearchResult,
    SourceReference,
)
from .orchestrator import ConfirmationRequired, Workflow, WorkspaceResolutionError
from .mcp import McpTapdClient, McpTapdError
from .dws import DwsClient, DwsError, GroupResolutionError
from .tapd import TapdApiError, TapdClient, TapdError
from .realtime import RealtimeEvent, RealtimeEventError, RealtimeEventListener, ResourceDownload
from .store import EventRecord, EventStore

__all__ = [
    "DingTalkGroup",
    "DingTalkMessage",
    "AutomationConfig",
    "AutoIssueService",
    "AutomationOutcome",
    "IssueAnalysis",
    "IssueDraft",
    "IssueType",
    "PaginationLedger",
    "SearchResult",
    "SourceReference",
    "ConfirmationRequired",
    "Workflow",
    "WorkspaceResolutionError",
    "McpTapdClient",
    "McpTapdError",
    "DwsClient",
    "DwsError",
    "GroupResolutionError",
    "RealtimeEvent",
    "RealtimeEventError",
    "RealtimeEventListener",
    "ResourceDownload",
    "EventRecord",
    "EventStore",
    "TapdClient",
    "TapdError",
    "TapdApiError",
]
