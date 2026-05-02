"""
db.py — SQLite session store

Stores per-chat settings and the Claude CLI session ID.
Conversation history is managed entirely by the Claude CLI (via --session-id / --resume).

Thread/coroutine safety: every public method opens its own short-lived connection
so concurrent asyncio tasks (enabled by concurrent_updates=True in bridge.py) never
share cursor state. SQLite's file-level locking serialises concurrent writes safely.
"""

import asyncio
import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_schema_sql = """
    CREATE TABLE IF NOT EXISTS sessions (
        chat_id           INTEGER PRIMARY KEY,
        model             TEXT    NOT NULL DEFAULT 'sonnet',
        claude_session_id TEXT,
        message_count     INTEGER NOT NULL DEFAULT 0,
        created_at        TEXT    NOT NULL,
        updated_at        TEXT    NOT NULL
    );

    CREATE TABLE IF NOT EXISTS exchanges (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id     INTEGER NOT NULL,
        role        TEXT    NOT NULL,
        content     TEXT    NOT NULL,
        model       TEXT,
        created_at  TEXT    NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_exchanges_chat ON exchanges(chat_id, created_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, data_dir: str):
        path = Path(data_dir)
        path.mkdir(parents=True, exist_ok=True)
        self.db_path = str(path / "bridge.db")
        self._lock = asyncio.Lock()
        self._init_schema()
        log.info(f"Database opened at {self.db_path}")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self):
        with self._connect() as conn:
            conn.executescript(_schema_sql)

    # ── Session access ──────────────────────────────────────────────────────

    async def get_session(self, chat_id: int, default_model: str) -> dict:
        async with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM sessions WHERE chat_id = ?", (chat_id,)
                ).fetchone()
                if row is None:
                    now = _now()
                    conn.execute(
                        """INSERT INTO sessions
                           (chat_id, model, claude_session_id, message_count, created_at, updated_at)
                           VALUES (?, ?, NULL, 0, ?, ?)""",
                        (chat_id, default_model, now, now),
                    )
                    return {
                        "chat_id": chat_id,
                        "model": default_model,
                        "claude_session_id": None,
                        "message_count": 0,
                    }
                return {
                    "chat_id": chat_id,
                    "model": row["model"],
                    "claude_session_id": row["claude_session_id"],
                    "message_count": row["message_count"],
                }

    async def set_claude_session_id(self, chat_id: int, session_id: str):
        async with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE sessions SET claude_session_id = ?, updated_at = ? WHERE chat_id = ?",
                    (session_id, _now(), chat_id),
                )

    async def increment_message_count(self, chat_id: int):
        async with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE sessions SET message_count = message_count + 1, updated_at = ? WHERE chat_id = ?",
                    (_now(), chat_id),
                )

    async def set_model(self, chat_id: int, model: str):
        async with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE sessions SET model = ?, updated_at = ? WHERE chat_id = ?",
                    (model, _now(), chat_id),
                )

    async def reset_session(self, chat_id: int):
        """Clear the Claude session ID — next message starts a fresh session.
        Model is preserved."""
        async with self._lock:
            with self._connect() as conn:
                conn.execute(
                    "UPDATE sessions SET claude_session_id = NULL, message_count = 0, updated_at = ? WHERE chat_id = ?",
                    (_now(), chat_id),
                )

    # ── Audit log ───────────────────────────────────────────────────────────

    async def log_exchange(self, chat_id: int, role: str, content: str, model: Optional[str] = None):
        async with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO exchanges (chat_id, role, content, model, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (chat_id, role, content, model, _now()),
                )
