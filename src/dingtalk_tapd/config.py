"""从环境变量读取外部服务配置，避免令牌进入源码、日志或提交历史。"""

from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse


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
class AgentConfig:
    """远端 Bug Agent 桥接配置；密钥只在进程内用于请求签名。"""

    events_url: str
    secret: str
    timeout_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> "AgentConfig":
        """读取公网桥接地址和共享密钥，未配置时明确阻止启动。"""

        url = os.getenv("DINGTALK_TAPD_AGENT_EVENTS_URL", "").strip()
        secret = os.getenv("DINGTALK_TAPD_AGENT_SECRET", "")
        if not url or not secret:
            raise ValueError("agent-listen 需要配置 DINGTALK_TAPD_AGENT_EVENTS_URL 和 DINGTALK_TAPD_AGENT_SECRET")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("DINGTALK_TAPD_AGENT_EVENTS_URL 必须是 HTTP(S) URL")
        raw_timeout = os.getenv("DINGTALK_TAPD_AGENT_TIMEOUT", "30")
        try:
            timeout_seconds = max(1.0, float(raw_timeout))
        except ValueError as exc:
            raise ValueError("DINGTALK_TAPD_AGENT_TIMEOUT 必须是数字") from exc
        return cls(url, secret, timeout_seconds)


def _parse_responsibility_whitelist(raw_value: str) -> tuple[dict[str, Any], ...]:
    """解析模块责任人白名单，统一兼容列表规则与模块名对象映射。"""

    source = raw_value.strip()
    if not source:
        return ()
    try:
        parsed = json.loads(source)
    except json.JSONDecodeError as exc:
        raise ValueError("DINGTALK_TAPD_RESPONSIBILITY_WHITELIST 必须是合法 JSON") from exc
    if isinstance(parsed, list):
        entries = parsed
    elif isinstance(parsed, dict):
        entries = [
            {"match": key, **(value if isinstance(value, dict) else {"owner": value})}
            for key, value in parsed.items()
        ]
    else:
        raise ValueError("DINGTALK_TAPD_RESPONSIBILITY_WHITELIST 必须是 JSON 数组或对象")
    normalized: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("DINGTALK_TAPD_RESPONSIBILITY_WHITELIST 的每条规则必须是对象")
        raw_matches = [entry.get(key) for key in ("match", "module", "modules", "keyword", "keywords")]
        matches = tuple(
            str(value).strip()
            for item in raw_matches
            for value in (item if isinstance(item, list) else (item,))
            if value is not None and str(value).strip()
        )
        def pick(*keys: str) -> str:
            """从中英文别名中取第一个非空责任人字段。"""

            return next(
                (str(entry[key]).strip() for key in keys if entry.get(key) is not None and str(entry[key]).strip()),
                "",
            )

        normalized.append(
            {
                "matches": matches,
                "owner": pick("owner", "current_owner", "currentOwner", "handler", "处理人"),
                "developer": pick("developer", "de", "developerName", "开发人"),
                "tester": pick("tester", "te", "testerName", "测试人"),
                "default": any(value.lower() in {"*", "default", "默认"} for value in matches),
            }
        )
    return tuple(normalized)


# 企业知识中心常见模块的默认分工；环境变量白名单有值时可覆盖这组规则。
DEFAULT_RESPONSIBILITY_WHITELIST: tuple[dict[str, Any], ...] = (
    {"matches": ("编译",), "owner": "杨耀发", "developer": "杨耀发", "tester": "", "default": False},
    {"matches": ("抽取",), "owner": "肖文杨", "developer": "肖文杨", "tester": "", "default": False},
    {"matches": ("本体",), "owner": "肖文杨", "developer": "肖文杨", "tester": "", "default": False},
    {"matches": ("对话部分", "对话"), "owner": "杨耀发", "developer": "杨耀发", "tester": "", "default": False},
)


# 默认保留历史目标群，并把新建的测试群加入同一套自动建单监听。
DEFAULT_AUTOMATION_GROUPS: tuple[tuple[str, str], ...] = (
    ("cid3SbKZNiotRpk9RdlluSUSA==", "DeepWorks 产品交流群"),
    ("cidfKBMe69X0qnHeBdQ9h7YNw==", "测试机器人"),
)


def _parse_group_targets(raw_value: str) -> tuple[tuple[str, str], ...]:
    """解析多群配置，兼容对象映射和带 id/name 字段的 JSON 数组。"""

    source = raw_value.strip()
    if not source:
        return ()
    try:
        parsed = json.loads(source)
    except json.JSONDecodeError as exc:
        raise ValueError("DINGTALK_TAPD_GROUPS 必须是合法 JSON") from exc
    if isinstance(parsed, Mapping):
        if any(key in parsed for key in ("id", "group_id", "groupId", "conversation_id", "openConversationId")):
            entries = [parsed]
        else:
            # 对象映射写法是 {"openConversationId": "群名"}，适合少量群配置。
            entries = [{"id": identifier, "name": name} for identifier, name in parsed.items()]
    elif isinstance(parsed, list):
        entries = parsed
    else:
        raise ValueError("DINGTALK_TAPD_GROUPS 必须是 JSON 数组或对象")

    groups: list[tuple[str, str]] = []
    seen: set[str] = set()
    for entry in entries:
        if isinstance(entry, Mapping):
            identifier = next(
                (entry.get(key) for key in ("id", "group_id", "groupId", "conversation_id", "openConversationId") if entry.get(key)),
                "",
            )
            name = next(
                (entry.get(key) for key in ("name", "group_name", "groupName", "title", "conversationName") if entry.get(key)),
                "",
            )
        elif isinstance(entry, (list, tuple)) and len(entry) == 2:
            identifier, name = entry
        else:
            raise ValueError("DINGTALK_TAPD_GROUPS 的每项必须是对象或 [id, name]")
        normalized_id = str(identifier or "").strip()
        normalized_name = str(name or "").strip()
        if not normalized_id or not normalized_name:
            raise ValueError("DINGTALK_TAPD_GROUPS 的每项都必须包含 id 和 name")
        if normalized_id in seen:
            continue
        seen.add(normalized_id)
        groups.append((normalized_id, normalized_name))
    if not groups:
        raise ValueError("DINGTALK_TAPD_GROUPS 至少需要一个群")
    return tuple(groups)


@dataclass(frozen=True, slots=True)
class AutomationConfig:
    """自动建单的业务默认值；环境变量只作为高级覆盖，不要求每次调用填写。"""

    # 保留旧字段供现有调用方使用；多群场景通过 group_targets 统一管理。
    group_id: str = DEFAULT_AUTOMATION_GROUPS[0][0]
    group_name: str = DEFAULT_AUTOMATION_GROUPS[0][1]
    workspace_id: str = "57379524"
    owner: str = "雷艾琳"
    # 开发人由白名单按模块覆盖；测试人当前统一使用雷艾琳，便于后续集中更新。
    developer: str = ""
    tester: str = "雷艾琳"
    responsibility_whitelist: tuple[dict[str, Any], ...] = DEFAULT_RESPONSIBILITY_WHITELIST
    title_prefix: str = "【企业知识中心—用户反馈】"
    state_db: str = ".dingtalk-tapd/state.sqlite3"
    attachment_dir: str = ".dingtalk-tapd/attachments"
    ready_timeout_seconds: float = 30.0
    ocr_command: str | None = None
    mention_targets: tuple[str, ...] = ("董超", "买年顺")
    mention_target_ids: tuple[str, ...] = ()
    # Bug Agent 单独使用机器人目标，避免迁移到群监听时误触发旧的人工 @ 规则。
    bot_mention_targets: tuple[str, ...] = ("缺陷机器人",)
    bot_mention_target_ids: tuple[str, ...] = ("6908187742", "DniS1GUKiiOoKA8NZrodrswyn6j1bSmGDQZ")
    # 新字段放在末尾，保持旧版按位置构造 AutomationConfig 的兼容性。
    group_targets: tuple[tuple[str, str], ...] | None = None

    def configured_groups(self) -> tuple[tuple[str, str], ...]:
        """返回去重后的目标群；旧版直接构造单群配置仍保持单群语义。"""

        if self.group_targets is not None:
            groups = self.group_targets
        elif (self.group_id, self.group_name) == DEFAULT_AUTOMATION_GROUPS[0]:
            groups = DEFAULT_AUTOMATION_GROUPS
        else:
            groups = ((self.group_id, self.group_name),)
        normalized: list[tuple[str, str]] = []
        seen: set[str] = set()
        for identifier, name in groups:
            normalized_id = str(identifier).strip()
            normalized_name = str(name).strip()
            if normalized_id and normalized_name and normalized_id not in seen:
                normalized.append((normalized_id, normalized_name))
                seen.add(normalized_id)
        return tuple(normalized)

    def is_target_group(self, conversation_id: str) -> bool:
        """按稳定会话 ID 判断事件是否来自任一配置目标群。"""

        return any(identifier == conversation_id for identifier, _ in self.configured_groups())

    def group_name_for(self, conversation_id: str) -> str | None:
        """按会话 ID 返回群名，供 TAPD 来源描述和日志使用。"""

        return next(
            (name for identifier, name in self.configured_groups() if identifier == conversation_id),
            None,
        )

    @classmethod
    def from_env(cls) -> "AutomationConfig":
        """读取自动流程覆盖项；默认同时兼容历史群和“测试机器人”群。"""

        defaults = cls()
        raw_groups = os.getenv("DINGTALK_TAPD_GROUPS", "").strip()
        if raw_groups:
            group_targets = _parse_group_targets(raw_groups)
        elif os.getenv("DINGTALK_TAPD_GROUP_ID") or os.getenv("DINGTALK_TAPD_GROUP_NAME"):
            # 旧版单群变量优先级更高，避免升级后意外扩大监听范围。
            group_targets = ((
                os.getenv("DINGTALK_TAPD_GROUP_ID", defaults.group_id).strip(),
                os.getenv("DINGTALK_TAPD_GROUP_NAME", defaults.group_name).strip(),
            ),)
        else:
            group_targets = defaults.configured_groups()
        if any(not identifier or not name for identifier, name in group_targets):
            raise ValueError("目标群配置必须同时包含群 ID 和群名")
        primary_group_id, primary_group_name = group_targets[0]
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
        mention_targets = tuple(
            dict.fromkeys(
                name.strip().lstrip("@")
                for name in os.getenv("DINGTALK_TAPD_MENTION_TARGETS", ",".join(defaults.mention_targets)).split(",")
                if name.strip().lstrip("@")
            )
        )
        if not mention_targets:
            raise ValueError("DINGTALK_TAPD_MENTION_TARGETS 至少需要一个姓名")
        mention_target_ids = tuple(
            dict.fromkeys(
                identifier.strip()
                for identifier in os.getenv("DINGTALK_TAPD_MENTION_TARGET_IDS", "").split(",")
                if identifier.strip()
            )
        )
        bot_mention_targets = tuple(
            dict.fromkeys(
                name.strip().lstrip("@")
                for name in os.getenv(
                    "DINGTALK_TAPD_BOT_MENTION_TARGETS", ",".join(defaults.bot_mention_targets)
                ).split(",")
                if name.strip().lstrip("@")
            )
        )
        if not bot_mention_targets:
            raise ValueError("DINGTALK_TAPD_BOT_MENTION_TARGETS 至少需要一个姓名")
        bot_mention_target_ids = tuple(
            dict.fromkeys(
                identifier.strip()
                for identifier in os.getenv(
                    "DINGTALK_TAPD_BOT_MENTION_TARGET_IDS", ",".join(defaults.bot_mention_target_ids)
                ).split(",")
                if identifier.strip()
            )
        )
        return cls(
            group_id=primary_group_id,
            group_name=primary_group_name,
            group_targets=group_targets,
            workspace_id=os.getenv("DINGTALK_TAPD_WORKSPACE_ID", defaults.workspace_id).strip(),
            owner=os.getenv("DINGTALK_TAPD_OWNER", defaults.owner).strip(),
            developer=os.getenv("DINGTALK_TAPD_DEVELOPER", defaults.developer).strip(),
            tester=os.getenv("DINGTALK_TAPD_TESTER", defaults.tester).strip() or defaults.tester,
            # 未配置环境变量时启用产品默认分工；显式传入 JSON（包括 []）仍可完全接管规则。
            responsibility_whitelist=(
                _parse_responsibility_whitelist(raw_responsibility)
                if (raw_responsibility := os.getenv("DINGTALK_TAPD_RESPONSIBILITY_WHITELIST", "").strip())
                else defaults.responsibility_whitelist
            ),
            title_prefix=os.getenv("DINGTALK_TAPD_TITLE_PREFIX", defaults.title_prefix),
            state_db=os.getenv("DINGTALK_TAPD_STATE_DB", defaults.state_db).strip(),
            attachment_dir=attachment_dir,
            ready_timeout_seconds=ready_timeout_seconds,
            ocr_command=os.getenv("DINGTALK_TAPD_OCR_COMMAND") or None,
            mention_targets=mention_targets,
            mention_target_ids=mention_target_ids,
            bot_mention_targets=bot_mention_targets,
            bot_mention_target_ids=bot_mention_target_ids,
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
