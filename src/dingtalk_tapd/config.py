"""从环境变量读取外部服务配置，避免令牌进入源码、日志或提交历史。"""

from __future__ import annotations

import os
from dataclasses import dataclass


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
class TapdConfig:
    """TAPD API 连接配置，优先使用个人访问令牌。"""

    api_base_url: str = "https://api.tapd.cn"
    base_url: str = "https://www.tapd.cn"
    access_token: str | None = None
    api_user: str | None = None
    api_password: str | None = None

    @classmethod
    def from_env(cls) -> "TapdConfig":
        """加载令牌或 Basic 认证配置，并保持敏感值只存在于进程内。"""

        return cls(
            api_base_url=os.getenv("TAPD_API_BASE_URL", "https://api.tapd.cn").rstrip("/"),
            base_url=os.getenv("TAPD_BASE_URL", "https://www.tapd.cn").rstrip("/"),
            access_token=os.getenv("TAPD_ACCESS_TOKEN") or None,
            api_user=os.getenv("TAPD_API_USER") or None,
            api_password=os.getenv("TAPD_API_PASSWORD") or None,
        )

    def validate(self) -> None:
        """在首次 API 调用前校验认证方式，避免发送无认证请求。"""

        if self.access_token:
            return
        if self.api_user and self.api_password:
            return
        raise ValueError("需要 TAPD_ACCESS_TOKEN 或 TAPD_API_USER/TAPD_API_PASSWORD")
