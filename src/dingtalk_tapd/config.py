"""从环境变量读取外部服务配置，避免令牌进入源码、日志或提交历史。"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DwsConfig:
    """DWS CLI 配置；profile 为空时沿用 CLI 当前 profile。"""

    executable: str = "dws"
    profile: str | None = None

    @classmethod
    def from_env(cls) -> "DwsConfig":
        """读取可选覆盖项，不读取或回显钉钉凭据。"""

        return cls(
            executable=os.getenv("DWS_EXECUTABLE", "dws"),
            profile=os.getenv("DWS_PROFILE") or None,
        )


@dataclass(frozen=True, slots=True)
class AutomationConfig:
    """自动建单的业务默认值；环境变量只作为高级覆盖，不要求每次调用填写。"""

    group_id: str = "cid3SbKZNiotRpk9RdlluSUSA=="
    group_name: str = "DeepWorks 产品交流群"
    workspace_id: str = "57379524"
    owner: str = "雷艾琳"
    title_prefix: str = "【用户反馈】"
    state_db: str = ".dingtalk-tapd/state.sqlite3"
    attachment_dir: str = ".dingtalk-tapd/attachments"
    ready_timeout_seconds: float = 30.0
    ocr_command: str | None = None

    @classmethod
    def from_env(cls) -> "AutomationConfig":
        """读取自动流程覆盖项；默认值对应已确认的 DeepWorks/TAPD 目标。"""

        defaults = cls()
        ready_timeout = os.getenv("DINGTALK_TAPD_READY_TIMEOUT", "30")
        try:
            ready_timeout_seconds = max(1.0, float(ready_timeout))
        except ValueError as exc:
            raise ValueError("DINGTALK_TAPD_READY_TIMEOUT 必须是数字") from exc
        attachment_dir = os.getenv("DINGTALK_TAPD_ATTACHMENT_DIR", defaults.attachment_dir).strip()
        # DWS 明确禁止绝对路径和 .. 逃逸，提前校验可避免监听到事件后才失败。
        attachment_path = Path(attachment_dir)
        if attachment_path.is_absolute() or ".." in attachment_path.parts:
            raise ValueError("DINGTALK_TAPD_ATTACHMENT_DIR 必须是工作目录内的相对路径")
        return cls(
            group_id=os.getenv("DINGTALK_TAPD_GROUP_ID", defaults.group_id).strip(),
            group_name=os.getenv("DINGTALK_TAPD_GROUP_NAME", defaults.group_name).strip(),
            workspace_id=os.getenv("DINGTALK_TAPD_WORKSPACE_ID", defaults.workspace_id).strip(),
            owner=os.getenv("DINGTALK_TAPD_OWNER", defaults.owner).strip(),
            title_prefix=os.getenv("DINGTALK_TAPD_TITLE_PREFIX", defaults.title_prefix),
            state_db=os.getenv("DINGTALK_TAPD_STATE_DB", defaults.state_db).strip(),
            attachment_dir=attachment_dir,
            ready_timeout_seconds=ready_timeout_seconds,
            ocr_command=os.getenv("DINGTALK_TAPD_OCR_COMMAND") or None,
        )


@dataclass(frozen=True, slots=True)
class TapdConfig:
    """TAPD 连接配置；CLI 环境配置默认通过官方 MCP 工具通信。"""

    api_base_url: str = "https://api.tapd.cn"
    base_url: str = "https://www.tapd.cn"
    access_token: str | None = None
    api_user: str | None = None
    api_password: str | None = None
    # 直接构造配置时保持 REST 兼容；CLI 的 from_env 默认覆盖为 MCP。
    backend: str = "rest"
    mcp_command: tuple[str, ...] = ("uvx", "mcp-server-tapd")

    @classmethod
    def from_env(cls) -> "TapdConfig":
        """加载令牌或 Basic 认证配置，并保持敏感值只存在于进程内。"""

        return cls(
            backend=os.getenv("TAPD_BACKEND", "mcp").strip().lower(),
            mcp_command=tuple(shlex.split(os.getenv("TAPD_MCP_COMMAND", "uvx mcp-server-tapd")))
            or ("uvx", "mcp-server-tapd"),
            api_base_url=os.getenv("TAPD_API_BASE_URL", "https://api.tapd.cn").rstrip("/"),
            base_url=os.getenv("TAPD_BASE_URL", "https://www.tapd.cn").rstrip("/"),
            access_token=os.getenv("TAPD_ACCESS_TOKEN") or None,
            api_user=os.getenv("TAPD_API_USER") or None,
            api_password=os.getenv("TAPD_API_PASSWORD") or None,
        )

    def validate(self) -> None:
        """在首次 API 调用前校验认证方式，避免发送无认证请求。"""

        if self.backend == "mcp":
            return
        if self.backend != "rest":
            raise ValueError("TAPD_BACKEND 只能是 mcp 或 rest")
        if self.access_token:
            return
        if self.api_user and self.api_password:
            return
        raise ValueError("需要 TAPD_ACCESS_TOKEN 或 TAPD_API_USER/TAPD_API_PASSWORD")
