"""把实时 @我 事件分析为企业知识中心 TAPD 缺陷并安全落账。"""

from __future__ import annotations

import json
import logging
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from .config import AutomationConfig
from .models import DingTalkGroup, IssueDraft, IssueType, SourceReference
from .orchestrator import Workflow
from .realtime import RealtimeEvent, ResourceDownload, fetch_message_details
from .store import EventStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IssueAnalysis:
    """分析器的稳定结果，供标题、优先级和描述共同消费。"""

    relevant: bool
    summary: str
    priority: str
    ocr_text: tuple[str, ...]
    media: tuple[dict[str, str], ...]
    description: str


@dataclass(frozen=True, slots=True)
class AutomationOutcome:
    """一次事件处理的可序列化结果，不包含任何认证信息。"""

    status: str
    event_key: str
    message_id: str
    title: str | None = None
    tapd_id: str | None = None
    tapd_url: str | None = None
    error: str | None = None
    attachment_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        """输出给 listen 命令的单行 JSON。"""

        return {
            "status": self.status,
            "eventKey": self.event_key,
            "messageId": self.message_id,
            "title": self.title,
            "tapdId": self.tapd_id,
            "tapdUrl": self.tapd_url,
            "error": self.error,
            "attachmentCount": self.attachment_count,
        }


class AttachmentInspector:
    """为本地图片尝试 OCR；没有 OCR 语言包时保留文件来源而不虚构内容。"""

    image_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".heic"}

    def __init__(self, command_template: str | None = None) -> None:
        self.command_template = command_template

    def inspect(self, downloads: tuple[ResourceDownload, ...]) -> tuple[str, ...]:
        """返回非空 OCR 文本；每个附件单独失败不会阻断整条建单流程。"""

        texts: list[str] = []
        for download in downloads:
            if not download.local_path:
                continue
            path = Path(download.local_path)
            if path.suffix.lower() not in self.image_suffixes or not path.is_file():
                continue
            text = self._ocr(path)
            if text:
                texts.append(f"{path.name}: {text}")
        return tuple(texts)

    def _ocr(self, path: Path) -> str:
        """调用本机 tesseract 或用户提供的模板命令，超时即放弃该图片。"""

        if self.command_template:
            try:
                command = [part.format(path=str(path)) for part in shlex.split(self.command_template)]
            except (KeyError, ValueError) as exc:
                logger.warning("忽略无效 OCR 命令模板: %s", exc)
                return ""
        else:
            executable = shutil.which("tesseract")
            if not executable:
                return ""
            command = [executable, str(path), "stdout", "-l", "eng"]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        if completed.returncode != 0:
            return ""
        return " ".join(completed.stdout.split())[:2000]


class IssueAnalyzer:
    """用可审计规则提取企业知识中心问题摘要与 TAPD 优先级。"""

    relevant_terms = ("企业知识中心", "企业知识", "知识中心", "知识库", "知识管理")
    urgent_terms = ("p0", "紧急", "线上全量", "数据丢失", "完全不可用")
    high_terms = ("失败", "报错", "异常", "无法", "卡死", "转圈", "崩溃", "不触发", "阻塞", "丢失")
    low_signal_terms = ("建议", "优化", "体验", "偶发", "咨询", "请问", "能否")

    def __init__(self, config: AutomationConfig) -> None:
        self.config = config
        self.attachments = AttachmentInspector(config.ocr_command)

    def analyze(self, event: RealtimeEvent, downloads: tuple[ResourceDownload, ...]) -> IssueAnalysis:
        """合并消息和 OCR 文本，生成可直接写入 TAPD 的分析结果。"""

        ocr_text = self.attachments.inspect(downloads)
        source_text = _clean_text(event.content)
        combined = "\n".join(part for part in (source_text, *ocr_text) if part)
        relevant = any(term in combined for term in self.relevant_terms)
        # @我 且只有截图时无法从正文判断关键词，但用户已明确把附件作为问题提交。
        if not source_text and downloads:
            relevant = True
        summary = self._summary(combined, bool(downloads))
        priority = self._priority(combined)
        media = _direct_media((*event.resource_refs, *(download.url or "" for download in downloads)))
        description = self._description(event, downloads, ocr_text, combined, self.config.group_name)
        return IssueAnalysis(relevant, summary, priority, ocr_text, media, description)

    def _summary(self, combined: str, has_attachments: bool) -> str:
        """把首句裁剪成稳定标题主体，避免把整段聊天复制到标题。"""

        if not combined:
            return "截图/附件中的企业知识中心问题" if has_attachments else "企业知识中心问题"
        first = re.split(r"[。！？!?\n]", combined, maxsplit=1)[0]
        first = re.sub(r"<at[^>]*>.*?</at>", "", first, flags=re.IGNORECASE)
        first = " ".join(first.split()).strip(" -:：")
        return first[:80] or "企业知识中心问题"

    def _priority(self, combined: str) -> str:
        """按影响词映射 TAPD 标准优先级；无法判断时使用中优先级而非伪造紧急程度。"""

        lowered = combined.lower()
        if any(term in lowered for term in self.urgent_terms):
            return "urgent"
        if any(term in lowered for term in self.high_terms):
            return "high"
        if any(term in lowered for term in self.low_signal_terms):
            return "low"
        return "medium"

    @staticmethod
    def _description(
        event: RealtimeEvent,
        downloads: tuple[ResourceDownload, ...],
        ocr_text: tuple[str, ...],
        combined: str,
        group_name: str,
    ) -> str:
        """生成包含来源、OCR 和下载 ledger 的 Markdown，确保附件失败可见。"""

        lines = ["## 问题摘要", "", event.content or "（消息正文为空，问题内容来自附件）"]
        if ocr_text:
            lines.extend(["", "## 截图文字识别", "", *[f"- {text}" for text in ocr_text]])
        if not ocr_text and downloads:
            lines.extend(["", "## 截图分析", "", "已下载截图；当前环境没有可用的中文视觉分析器，保留原图供 TAPD 查看。"])
        lines.extend(
            [
                "",
                "## 来源",
                f"- group：{group_name}",
                f"- conversationId：`{event.conversation_id}`",
                f"- messageId：`{event.message_id}`",
                f"- sender：{event.sender_name}",
                f"- createdAt：{event.created_at}",
            ]
        )
        if combined and not event.content:
            lines.extend(["", "## 组合分析文本", "", combined])
        if downloads:
            lines.extend(["", "## 附件"])
            for download in downloads:
                status = f"本地文件：`{download.local_path}`" if download.succeeded else "下载失败"
                if download.error:
                    status += f"（{download.error}）"
                if download.url:
                    status += f"；直链：{download.url}"
                lines.append(f"- `{download.resource_id}`：{status}")
        direct_refs = tuple(ref for ref in event.resource_refs if ref.startswith(("http://", "https://")))
        if direct_refs:
            lines.extend(["", "## 附件直链", *[f"- {ref}" for ref in direct_refs]])
        return "\n".join(lines)


def _clean_text(value: str) -> str:
    """清理事件中的 @ 标记和重复空白，保留用户原始问题语义。"""

    value = re.sub(r"<at[^>]*>.*?</at>", " ", value or "", flags=re.IGNORECASE)
    return " ".join(value.split()).strip()


def _direct_media(refs: tuple[str, ...]) -> tuple[dict[str, str], ...]:
    """仅把真实 HTTP(S) 直链交给 TAPD 富媒体渲染，不把本地路径伪装成 URL。"""

    media: list[dict[str, str]] = []
    seen: set[str] = set()
    video_suffixes = {".mp4", ".mov", ".webm", ".avi", ".mkv"}
    for ref in refs:
        parsed = urlparse(ref)
        if parsed.scheme not in {"http", "https"} or ref in seen:
            continue
        seen.add(ref)
        media_type = "video" if Path(parsed.path).suffix.lower() in video_suffixes else "image"
        media.append({"type": media_type, "url": ref, "alt": "钉钉附件"})
    return tuple(media)


def _tapd_reference(value: Any) -> tuple[str | None, str | None]:
    """从 MCP/REST 多种返回包装中提取 TAPD ID 与链接。"""

    found_id: str | None = None
    found_url: str | None = None

    def visit(item: Any) -> None:
        nonlocal found_id, found_url
        if isinstance(item, str):
            try:
                parsed = json.loads(item)
            except json.JSONDecodeError:
                return
            visit(parsed)
            return
        if isinstance(item, Mapping):
            for key in ("url", "tapd_url", "tapdUrl"):
                raw = item.get(key)
                if isinstance(raw, str) and raw.startswith(("http://", "https://")):
                    found_url = found_url or raw
            for key in ("bug_id", "bugId", "id"):
                raw = item.get(key)
                if raw is not None and str(raw).strip() and found_id is None:
                    found_id = str(raw).strip()
            for nested in item.values():
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)

    visit(value)
    return found_id, found_url


class AutoIssueService:
    """消费 @我 事件并自动创建 TAPD Bug，默认不需要用户填写字段。"""

    def __init__(self, workflow: Workflow, config: AutomationConfig) -> None:
        self.workflow = workflow
        self.config = config
        self.analyzer = IssueAnalyzer(config)
        self.store = EventStore(config.state_db)

    def process(self, event: RealtimeEvent) -> AutomationOutcome:
        """过滤目标群和主题，完成一次有幂等保护的自动建单。"""

        event_key = f"{event.conversation_id}:{event.message_id}"
        if event.conversation_id != self.config.group_id:
            return AutomationOutcome("ignored", event_key, event.message_id, error="非目标 DeepWorks 群")
        if not self.store.claim(event_key):
            return AutomationOutcome("duplicate", event_key, event.message_id, error="事件已处理")

        downloads: tuple[ResourceDownload, ...] = ()
        detail_error: str | None = None
        try:
            _, downloads = fetch_message_details(self.workflow.dws, event, self.config)
        except Exception as exc:  # noqa: BLE001 - 详情失败仍保留事件并交给分析/人工核对
            detail_error = f"消息详情或附件下载失败：{exc}"
            logger.warning(detail_error)

        analysis = self.analyzer.analyze(event, downloads)
        if not analysis.relevant:
            self.store.mark_ignored(event_key, "与企业知识中心无关")
            return AutomationOutcome("ignored", event_key, event.message_id, error="与企业知识中心无关")

        title = f"{self.config.title_prefix}{analysis.summary}-{_timestamp()}"
        description = analysis.description
        if detail_error:
            description += f"\n\n## 处理告警\n- {detail_error}"
        draft = IssueDraft(
            issue_type=IssueType.BUG,
            workspace_id=self.config.workspace_id,
            title=title,
            description=description,
            sources=(
                SourceReference(
                    conversation_id=event.conversation_id,
                    message_id=event.message_id,
                    sender_name=event.sender_name,
                    created_at=event.created_at,
                    resource_refs=event.resource_refs,
                ),
            ),
            fields={
                "owner": self.config.owner,
                "priority": analysis.priority,
                **({"media": list(analysis.media)} if analysis.media else {}),
            },
        )
        try:
            # 自动入口已经由用户 @ 触发；仍读取项目元数据/字段作为写前契约校验。
            self.workflow.confirmation_context(draft)
            response = self.workflow.create(draft, confirmed=True)
            tapd_id, tapd_url = _tapd_reference(response)
            verification_error = self._verify(tapd_id)
            self.store.mark_created(event_key, tapd_id, tapd_url)
            return AutomationOutcome(
                "created",
                event_key,
                event.message_id,
                title,
                tapd_id,
                tapd_url,
                verification_error,
                len(downloads),
            )
        except Exception as exc:  # noqa: BLE001 - 状态账本需要记录所有可见业务错误
            error = str(exc)
            self.store.mark_failed(event_key, error)
            return AutomationOutcome("failed", event_key, event.message_id, title, error=error, attachment_count=len(downloads))

    def _verify(self, tapd_id: str | None) -> str | None:
        """写入后调用可用的 get_bug；无法提取 ID 时返回解释而不阻断已完成写入。"""

        if not tapd_id:
            return "TAPD 已返回但未提取到缺陷 ID，未执行二次核对"
        verifier = getattr(self.workflow.tapd, "get_bug", None)
        if not callable(verifier):
            return "当前 TAPD 后端不提供 get_bug，未执行二次核对"
        try:
            verifier(self.config.workspace_id, tapd_id)
        except Exception as exc:  # noqa: BLE001 - 不把已成功写入标成可重试失败
            return f"TAPD 写入后核对失败：{exc}"
        return None

    def close(self) -> None:
        """关闭幂等账本连接。"""

        self.store.close()


def _timestamp() -> str:
    """使用用户所在的中国时区生成可读时间戳，避免服务器 UTC 造成标题错日。"""

    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d-%H%M%S")
