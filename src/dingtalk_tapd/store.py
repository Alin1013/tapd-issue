"""保存实时事件的幂等状态，避免进程重启后重复创建 TAPD 工单。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True, slots=True)
class EventRecord:
    """实时事件当前状态的只读快照。"""

    event_key: str
    status: str
    tapd_id: str | None
    tapd_url: str | None
    error: str | None
    updated_at: str


class EventStore:
    """基于 SQLite 的轻量幂等账本；INSERT OR IGNORE 把并发竞态交给数据库。"""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        self._connection.execute("PRAGMA busy_timeout = 10000")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                event_key TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                tapd_id TEXT,
                tapd_url TEXT,
                error TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )

    @staticmethod
    def _now() -> str:
        """统一使用 UTC 存储更新时间，避免夏令时或本地时区改变导致账本混乱。"""

        return datetime.now(timezone.utc).isoformat()

    def claim(self, event_key: str) -> bool:
        """尝试占有事件；任何已有状态都不再次建单，保障写入幂等。"""

        if not event_key.strip():
            raise ValueError("event_key 不能为空")
        cursor = self._connection.execute(
            "INSERT OR IGNORE INTO events(event_key, status, updated_at) VALUES (?, 'processing', ?)",
            (event_key, self._now()),
        )
        return cursor.rowcount == 1

    def mark_ignored(self, event_key: str, reason: str) -> None:
        """记录不相关事件的原因，防止同一事件反复分析。"""

        self._update(event_key, status="ignored", error=reason)

    def mark_created(self, event_key: str, tapd_id: str | None, tapd_url: str | None) -> None:
        """在 TAPD 写接口返回后落盘成功结果，即使后续验证失败也不重试写入。"""

        self._update(event_key, status="created", tapd_id=tapd_id, tapd_url=tapd_url, error=None)

    def mark_failed(self, event_key: str, error: str) -> None:
        """记录失败供人工排查；不自动重试未知写入结果以避免重复工单。"""

        self._update(event_key, status="failed", error=error)

    def get(self, event_key: str) -> EventRecord | None:
        """读取事件状态，供运维或后续补偿流程检查。"""

        row = self._connection.execute(
            "SELECT event_key, status, tapd_id, tapd_url, error, updated_at FROM events WHERE event_key = ?",
            (event_key,),
        ).fetchone()
        return EventRecord(*row) if row else None

    def _update(
        self,
        event_key: str,
        *,
        status: str,
        tapd_id: str | None = None,
        tapd_url: str | None = None,
        error: str | None = None,
    ) -> None:
        """集中更新状态，确保每次变更都刷新可审计时间。"""

        self._connection.execute(
            """
            UPDATE events
               SET status = ?, tapd_id = ?, tapd_url = ?, error = ?, updated_at = ?
             WHERE event_key = ?
            """,
            (status, tapd_id, tapd_url, error, self._now(), event_key),
        )

    def close(self) -> None:
        """关闭数据库连接，允许 CLI 在退出时及时释放文件句柄。"""

        self._connection.close()

    def __enter__(self) -> "EventStore":
        """支持上下文管理，异常路径也能关闭 SQLite。"""

        return self

    def __exit__(self, *_: object) -> None:
        """离开上下文时关闭连接。"""

        self.close()
