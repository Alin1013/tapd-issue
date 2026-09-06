"""把 DWS 群消息和本地媒体安全转发到远端 Bug Agent。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import time
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import AgentConfig, AutomationConfig
from .realtime import RealtimeEvent, ResourceDownload, fetch_message_details


# 与远端服务的 MAX_MEDIA_FILES 保持一致，避免桥接请求在服务端被整批拒绝。
MAX_AGENT_MEDIA_FILES = 5


class AgentBridgeError(RuntimeError):
    """远端 Agent 桥接请求失败，错误信息不包含签名密钥。"""


class AgentEventForwarder:
    """下载 DWS 事件资源并以签名 JSON 请求交给远端 Agent。"""

    def __init__(self, dws: Any, agent: AgentConfig, automation: AutomationConfig) -> None:
        self.dws = dws
        self.agent = agent
        self.automation = automation
        # 先发文字、后发截图是群聊中常见的操作，按发送人和会话短暂合并上下文。
        self._text_context: dict[str, tuple[str, float]] = {}

    def process(self, event: RealtimeEvent) -> dict[str, Any]:
        """处理一条目标群事件；远端返回 ``accepted`` 或 ``duplicate`` 状态。"""

        if event.conversation_id != self.automation.group_id:
            return {"status": "ignored", "messageId": event.message_id, "reason": "非目标群"}
        if not event.is_automation_trigger(
            self.automation.bot_mention_targets,
            self.automation.bot_mention_target_ids,
        ):
            return {"status": "ignored", "messageId": event.message_id, "reason": "未命中 @ 触发"}

        downloads: tuple[ResourceDownload, ...] = ()
        detail_error: str | None = None
        try:
            _, downloads = fetch_message_details(self.dws, event, self.automation)
        except Exception as exc:  # noqa: BLE001 - 转发失败需让监听日志和上层重试可见
            detail_error = f"消息媒体下载失败：{exc}"
        if not downloads and not event.resource_refs and event.content.strip():
            # 纯文字描述本身就是完整建单输入，立即转发；远端会在没有媒体时直接调用模型分析。
            source_text = "\n".join(filter(None, (self._consume_text(event), event.content.strip())))
            payload = {
                "eventId": f"{event.conversation_id}:{event.message_id}",
                "messageId": event.message_id,
                "conversationId": event.conversation_id,
                "senderStaffId": event.sender_identifier(),
                "senderName": event.sender_name,
                "conversationType": event.conversation_type(),
                "sessionWebhook": event.session_webhook(),
                "createdAt": event.created_at,
                "text": event.content,
                "sourceText": source_text,
                "resourceRefs": [],
                "media": [],
                "warnings": [],
            }
            return self._post(payload)
        if event.resource_refs and not downloads:
            return {
                "status": "failed",
                "messageId": event.message_id,
                "reason": detail_error or "截图或视频下载失败",
            }
        if not downloads and not event.resource_refs:
            return {"status": "ignored", "messageId": event.message_id, "reason": "未找到截图或视频"}
        source_text = "\n".join(filter(None, (self._consume_text(event), event.content.strip())))
        sender_id = event.sender_identifier()
        payload = {
            "eventId": f"{event.conversation_id}:{event.message_id}",
            "messageId": event.message_id,
            "conversationId": event.conversation_id,
            "senderStaffId": sender_id,
            "senderName": event.sender_name,
            "conversationType": event.conversation_type(),
            "sessionWebhook": event.session_webhook(),
            "createdAt": event.created_at,
            "text": event.content,
            "sourceText": source_text,
            "resourceRefs": list(event.resource_refs),
            "media": self._encode_downloads(downloads),
            "warnings": [detail_error] if detail_error else [],
        }
        return self._post(payload)

    def _context_key(self, event: RealtimeEvent) -> str:
        """生成同一发送人/会话的上下文键，不依赖显示名的唯一性。"""

        return f"{event.conversation_id}:{event.sender_identifier() or event.sender_name}"

    def _remember_text(self, event: RealtimeEvent) -> None:
        """保存短期文字上下文，并清理过期条目避免常驻监听器无限增长。"""

        now = time.time()
        self._text_context = {key: value for key, value in self._text_context.items() if value[1] > now}
        self._text_context[self._context_key(event)] = (event.content.strip()[:8000], now + 120)

    def _consume_text(self, event: RealtimeEvent) -> str:
        """取出并删除待合并文本，确保一段上下文只进入一条媒体事件。"""

        item = self._text_context.pop(self._context_key(event), None)
        return item[0] if item and item[1] > time.time() else ""

    @staticmethod
    def _encode_downloads(downloads: tuple[ResourceDownload, ...]) -> list[dict[str, str]]:
        """只转发已落盘且在单次总量限制内的媒体，避免把本地路径暴露给公网。"""

        encoded: list[dict[str, str]] = []
        total_bytes = 0
        for download in downloads:
            if len(encoded) >= MAX_AGENT_MEDIA_FILES:
                break
            if not download.succeeded or not download.local_path:
                continue
            path = Path(download.local_path)
            try:
                size = path.stat().st_size
            except OSError:
                # DWS 可能在监听器清理目录时并发删除文件；单个资源消失不应丢掉整条事件。
                continue
            if size > 8 * 1024 * 1024 or total_bytes + size > 12 * 1024 * 1024:
                continue
            mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if mime_type not in {
                "image/png", "image/jpeg", "image/webp", "image/gif",
                "video/mp4", "video/webm", "video/quicktime",
            }:
                continue
            encoded.append({
                "name": path.name,
                "contentType": mime_type,
                "data": f"data:{mime_type};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}",
            })
            total_bytes += size
        return encoded

    def _post(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """使用时间戳和请求体签名调用远端入口，并限制响应大小。"""

        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        timestamp = str(int(time.time() * 1000))
        signature = hmac.new(
            self.agent.secret.encode("utf-8"),
            f"{timestamp}\n".encode("utf-8") + body,
            hashlib.sha256,
        ).hexdigest()
        request = Request(
            self.agent.events_url,
            data=body,
            headers={
                "content-type": "application/json; charset=utf-8",
                "x-agent-timestamp": timestamp,
                "x-agent-signature": signature,
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.agent.timeout_seconds) as response:
                raw = response.read(256 * 1024)
        except HTTPError as exc:
            detail = exc.read(2048).decode("utf-8", "replace")
            raise AgentBridgeError(f"远端 Bug Agent 返回 HTTP {exc.code}: {detail[:300]}") from exc
        except (OSError, URLError, TimeoutError) as exc:
            raise AgentBridgeError(f"远端 Bug Agent 请求失败：{exc}") from exc
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentBridgeError("远端 Bug Agent 返回不是合法 JSON") from exc
        if not isinstance(result, dict):
            raise AgentBridgeError("远端 Bug Agent 返回不是 JSON 对象")
        if result.get("ok") is False:
            raise AgentBridgeError(str(result.get("error") or "远端 Bug Agent 拒绝事件"))
        return result
