"""通过 MCP stdio 协议调用官方 mcp-server-tapd 工具。"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any, Mapping

from .config import TapdConfig
from .models import IssueDraft, IssueType
from .tapd import TapdError


class McpTapdError(TapdError):
    """MCP 进程启动、协议通信或工具调用失败。"""


@dataclass(slots=True)
class McpTapdClient:
    """启动一个短生命周期 MCP Server，并暴露与 TapdClient 相同的业务方法。"""

    config: TapdConfig
    timeout_seconds: float = 30.0
    _process: subprocess.Popen[bytes] | None = field(default=None, init=False, repr=False)
    _next_id: int = field(default=0, init=False, repr=False)

    def _ensure_started(self) -> None:
        """按需启动 stdio server 并完成 MCP initialize 握手。"""

        if self._process is not None:
            return
        try:
            # 官方服务在仅配置令牌时会因缺少 API 基地址递归初始化；显式注入默认值避免启动失败。
            child_env = os.environ.copy()
            child_env.setdefault("TAPD_API_BASE_URL", self.config.api_base_url)
            child_env.setdefault("TAPD_BASE_URL", self.config.base_url)
            self._process = subprocess.Popen(
                list(self.config.mcp_command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=child_env,
            )
        except OSError as exc:
            raise McpTapdError(f"无法启动 TAPD MCP Server: {' '.join(self.config.mcp_command)}") from exc
        self._rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "dingtalk-tapd-bridge", "version": "0.1.0"},
            },
        )
        self._notify("notifications/initialized")

    def _send(self, message: Mapping[str, Any]) -> None:
        """按 MCP stdio 的单行 JSON 约定发送一条 JSON-RPC 消息。"""

        if self._process is None or self._process.stdin is None:
            raise McpTapdError("MCP Server 尚未启动")
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._process.stdin.write(encoded + b"\n")
        self._process.stdin.flush()

    def _read(self) -> dict[str, Any]:
        """读取一帧 JSON-RPC；同时兼容开发工具使用的单行 JSON 输出。"""

        if self._process is None or self._process.stdout is None:
            raise McpTapdError("MCP Server 尚未启动")
        first = self._process.stdout.readline()
        if not first:
            raise McpTapdError("TAPD MCP Server 提前退出")
        if first.lstrip().startswith(b"{"):
            raw = first
        else:
            headers = first
            while headers.strip():
                line = self._process.stdout.readline()
                if not line:
                    raise McpTapdError("MCP 响应头不完整")
                headers += line
                if line in (b"\r\n", b"\n"):
                    break
            length = None
            for line in headers.splitlines():
                name, _, value = line.partition(b":")
                if name.lower() == b"content-length":
                    length = int(value.strip())
                    break
            if length is None:
                raise McpTapdError("MCP 响应缺少 Content-Length")
            raw = self._process.stdout.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise McpTapdError("MCP 响应不是合法 JSON") from exc
        if not isinstance(payload, dict):
            raise McpTapdError("MCP 响应不是 JSON 对象")
        return payload

    def _rpc(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        """发送请求并跳过服务端通知，直到收到匹配 request ID。"""

        self._next_id += 1
        request_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        while True:
            response = self._read()
            if response.get("id") != request_id:
                continue
            if "error" in response:
                error = response["error"]
                detail = error.get("message", "未知错误") if isinstance(error, Mapping) else str(error)
                raise McpTapdError(f"MCP 方法 {method} 失败: {detail}")
            return response.get("result")

    def _notify(self, method: str) -> None:
        """发送无需响应的 MCP 通知。"""

        self._send({"jsonrpc": "2.0", "method": method})

    def _call_tool(self, name: str, arguments: Mapping[str, Any]) -> Any:
        """调用 TAPD 工具并解析 structuredContent 或文本 JSON 结果。"""

        self._ensure_started()
        result = self._rpc("tools/call", {"name": name, "arguments": dict(arguments)})
        if not isinstance(result, Mapping):
            raise McpTapdError(f"MCP 工具 {name} 返回空结果")
        if result.get("isError"):
            raise McpTapdError(f"TAPD MCP 工具 {name} 返回业务错误")
        structured = result.get("structuredContent")
        if structured is not None:
            # 官方服务的 structuredContent 在部分版本会再包一层 JSON 字符串。
            if isinstance(structured, Mapping) and isinstance(structured.get("result"), str):
                try:
                    return json.loads(structured["result"])
                except json.JSONDecodeError:
                    return structured["result"]
            return structured
        content = result.get("content")
        if isinstance(content, list):
            texts = [
                item.get("text")
                for item in content
                if isinstance(item, Mapping) and isinstance(item.get("text"), str)
            ]
            if len(texts) == 1:
                try:
                    return json.loads(texts[0])
                except json.JSONDecodeError:
                    return texts[0]
            if texts:
                return texts
        return result

    def close(self) -> None:
        """关闭本客户端启动的 MCP 进程，避免 CLI 退出后残留子进程。"""

        process, self._process = self._process, None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

    def get_user_participant_projects(self, user_name: str) -> Any:
        """调用 MCP 项目参与查询工具。"""

        # 官方工具参数名是 nick；适配器对外仍保留 user_name 语义，避免 CLI 接口变化。
        return self._call_tool("get_user_participant_projects", {"nick": user_name})

    def get_workspace_info(self, workspace_id: str) -> Any:
        """调用 MCP workspace 元数据工具。"""

        return self._call_tool("get_workspace_info", {"workspace_id": workspace_id})

    def get_entity_custom_fields(self, workspace_id: str, entity_type: IssueType) -> Any:
        """按文档约定传递实体类型，读取字段定义供确认与校验。"""

        # TAPD 的缺陷字段接口使用复数 bugs，领域枚举仍保留 bug 以匹配 CLI 对外契约。
        api_entity_type = "bugs" if entity_type is IssueType.BUG else entity_type.value
        return self._call_tool(
            "get_entity_custom_fields",
            {"workspace_id": workspace_id, "entity_type": api_entity_type},
        )

    def create_bug(self, draft: IssueDraft) -> Any:
        """调用 create_bug 工具，使用 options 承载描述和字段。"""

        if draft.issue_type is not IssueType.BUG:
            raise ValueError("create_bug 只接受 bug 草稿")
        return self._call_tool(
            "create_bug",
            {"workspace_id": draft.workspace_id, "title": draft.title, "options": self._options(draft)},
        )

    def create_story_or_task(self, draft: IssueDraft) -> Any:
        """调用 create_story_or_task 工具并明确传递 stories/tasks。"""

        if draft.issue_type not in {IssueType.STORY, IssueType.TASK}:
            raise ValueError("create_story_or_task 只接受 stories 或 tasks 草稿")
        options = self._options(draft)
        options["entity_type"] = draft.issue_type.value
        return self._call_tool(
            "create_story_or_task",
            {"workspace_id": draft.workspace_id, "name": draft.title, "options": options},
        )

    def create_comments(self, workspace_id: str, entry_type: IssueType, entry_id: str, text: str) -> Any:
        """调用 MCP 评论工具，把来源补充到已存在的实体。"""

        return self._call_tool(
            "create_comments",
            {
                "workspace_id": workspace_id,
                "entry_type": entry_type.value,
                "entry_id": entry_id,
                "description": text,
            },
        )

    def get_bug(self, workspace_id: str, bug_id: str) -> Any:
        """调用 MCP 缺陷查询工具。"""

        return self._call_tool("get_bug", {"workspace_id": workspace_id, "options": {"id": bug_id}})

    def get_stories_or_tasks(self, workspace_id: str, entity_type: IssueType, entity_id: str) -> Any:
        """调用 MCP 需求/任务查询工具。"""

        return self._call_tool(
            "get_stories_or_tasks",
            {
                "workspace_id": workspace_id,
                "options": {"entity_type": entity_type.value, "id": entity_id},
            },
        )

    @staticmethod
    def _options(draft: IssueDraft) -> dict[str, Any]:
        """将基础字段和 custom_fields 分层，匹配 MCP options 契约。"""

        basic_keys = {"owner", "priority", "severity"}
        options: dict[str, Any] = {"description": draft.description}
        # TAPD MCP 的缺陷字段名与项目 CLI 的友好参数名不同，这里集中完成映射。
        field_aliases = {"owner": "current_owner", "priority": "priority_label", "severity": "severity"}
        options.update({field_aliases[key]: value for key, value in draft.fields.items() if key in basic_keys})
        custom_fields = {key: value for key, value in draft.fields.items() if key not in basic_keys}
        if custom_fields:
            options["custom_fields"] = custom_fields
        return options

    def __del__(self) -> None:
        """在解释器回收时尽力释放 MCP 子进程，显式 close 仍是首选。"""

        try:
            self.close()
        except Exception:
            pass
