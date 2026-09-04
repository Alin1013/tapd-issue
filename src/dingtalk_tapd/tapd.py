"""通过 TAPD REST API 查询项目元数据并创建可追溯工单。"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping

from .config import TapdConfig
from .models import IssueDraft, IssueType


class TapdError(RuntimeError):
    """TAPD 请求或响应失败时抛出的基础异常。"""


class TapdApiError(TapdError):
    """TAPD API 返回错误状态或业务失败状态。"""


@dataclass(slots=True)
class TapdClient:
    """TAPD API 客户端，统一认证、序列化和响应解包逻辑。"""

    config: TapdConfig
    timeout_seconds: float = 30.0

    def _headers(self) -> dict[str, str]:
        """生成认证头；令牌值只进入请求头，不写入异常消息。"""

        self.config.validate()
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "dingtalk-tapd-bridge/0.1",
        }
        if self.config.access_token:
            headers["Authorization"] = f"Bearer {self.config.access_token}"
        else:
            raw = f"{self.config.api_user}:{self.config.api_password}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        return headers

    @staticmethod
    def _decode_response(raw: bytes) -> Any:
        """解码 JSON，并让调用方看到稳定的对象/数组而非字节流。"""

        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TapdApiError("TAPD 返回的不是合法 JSON") from exc

    @staticmethod
    def _unwrap(payload: Any) -> Any:
        """处理 TAPD 常见的 data 包装，并检查显式失败状态。"""

        if not isinstance(payload, Mapping):
            return payload
        status = payload.get("status")
        if status not in (None, 1, "1", "success", "ok", True):
            message = payload.get("message") or payload.get("info") or "未知业务错误"
            raise TapdApiError(f"TAPD 业务失败: {message}")
        return payload.get("data", payload)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        form: Mapping[str, Any] | None = None,
    ) -> Any:
        """发送单次 API 请求；GET 参数和写入表单分别编码，避免手工拼接 URL。"""

        if not path.startswith("/"):
            raise ValueError("TAPD API path 必须以 / 开头")
        query = urllib.parse.urlencode({key: value for key, value in (params or {}).items() if value is not None})
        url = f"{self.config.api_base_url}{path}"
        if query:
            url += "?" + query
        data = None
        if form is not None:
            data = urllib.parse.urlencode({key: value for key, value in form.items() if value is not None}).encode()
        request = urllib.request.Request(url, data=data, headers=self._headers(), method=method.upper())
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = self._decode_response(response.read())
        except urllib.error.HTTPError as exc:
            # 响应体通常包含可读业务错误，但不回显请求头中的凭据。
            body = exc.read()
            try:
                detail = self._unwrap(self._decode_response(body))
            except TapdError:
                detail = body.decode("utf-8", errors="replace")[:500]
            raise TapdApiError(f"TAPD HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise TapdApiError(f"TAPD 网络请求失败: {exc.reason}") from exc
        except TimeoutError as exc:
            raise TapdApiError("TAPD 请求超时") from exc
        return self._unwrap(payload)

    def get_user_participant_projects(self, user_name: str) -> Any:
        """按用户昵称查询其参与项目，供 workspace 消歧使用。"""

        if not user_name.strip():
            raise ValueError("TAPD 用户昵称不能为空")
        return self._request("GET", "/workspaces", params={"user_name": user_name})

    def get_workspace_info(self, workspace_id: str) -> Any:
        """读取项目基本信息，写入前用于确认目标 workspace。"""

        if not workspace_id.strip():
            raise ValueError("workspace_id 不能为空")
        return self._request("GET", "/workspace/info", params={"workspace_id": workspace_id})

    def get_entity_custom_fields(self, workspace_id: str, entity_type: IssueType) -> Any:
        """读取工单实体的自定义字段，避免调用方猜字段别名和值。"""

        endpoints = {
            IssueType.BUG: "/bug/custom_fields",
            IssueType.STORY: "/story/custom_fields",
            IssueType.TASK: "/task/custom_fields",
        }
        return self._request(
            "GET",
            endpoints[entity_type],
            params={"workspace_id": workspace_id},
        )

    def create_bug(self, draft: IssueDraft) -> Any:
        """创建 TAPD 缺陷；调用方应在此之前完成用户确认。"""

        if draft.issue_type is not IssueType.BUG:
            raise ValueError("create_bug 只接受 bug 草稿")
        return self._request("POST", "/bugs", form=self._issue_form(draft, title_key="title"))

    def create_story_or_task(self, draft: IssueDraft) -> Any:
        """按 stories/tasks 类型创建需求或任务，并保留来源描述。"""

        if draft.issue_type not in {IssueType.STORY, IssueType.TASK}:
            raise ValueError("create_story_or_task 只接受 stories 或 tasks 草稿")
        path = "/stories" if draft.issue_type is IssueType.STORY else "/tasks"
        return self._request("POST", path, form=self._issue_form(draft, title_key="name"))

    def create_comments(self, workspace_id: str, entry_type: IssueType, entry_id: str, text: str) -> Any:
        """给已有 TAPD 实体追加评论，评论内容由上层负责去重和确认。"""

        if not entry_id.strip() or not text.strip():
            raise ValueError("评论必须包含实体 ID 和内容")
        return self._request(
            "POST",
            "/comments",
            form={
                "workspace_id": workspace_id,
                "entry_type": entry_type.value,
                "entry_id": entry_id,
                "description": text,
            },
        )

    def get_bug(self, workspace_id: str, bug_id: str) -> Any:
        """读取已创建缺陷，作为写入后的核对接口。"""

        return self._request("GET", "/bug", params={"workspace_id": workspace_id, "id": bug_id})

    def get_stories_or_tasks(self, workspace_id: str, entity_type: IssueType, entity_id: str) -> Any:
        """读取已创建需求或任务，作为写入后的核对接口。"""

        if entity_type not in {IssueType.STORY, IssueType.TASK}:
            raise ValueError("entity_type 必须是 stories 或 tasks")
        path = "/story" if entity_type is IssueType.STORY else "/task"
        return self._request("GET", path, params={"workspace_id": workspace_id, "id": entity_id})

    @staticmethod
    def _issue_form(draft: IssueDraft, *, title_key: str) -> dict[str, Any]:
        """把草稿转换为 TAPD 表单，并使用 custom_fields 显式传递扩展字段。"""

        form: dict[str, Any] = {
            "workspace_id": draft.workspace_id,
            title_key: draft.title,
            "description": draft.description,
        }
        form.update(draft.fields)
        return form
