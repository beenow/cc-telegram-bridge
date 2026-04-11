"""
db.py — SQLite session store

Stores per-chat settings and the Claude CLI session ID.
Conversation history is managed entirely by the Claude CLI (via --session-id / --resume).
"""

import json
import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, data_dir: str):
        path = Path(data_dir)
        path.mkdir(parents=True, exist_ok=True)
        self.db_path = str(path / "bridge.db")
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()
        log.info(f"Database opened at {self.db_path}")

    def _init_schema(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                chat_id           INTEGER PRIMARY KEY,
                model             TEXT    NOT NULL DEFAULT 'sonnet',
                tools_enabled     INTEGER NOT NULL DEFAULT 0,
                claude_session_id TEXT,              -- UUID used with claude --session-id / --resume
                message_count     INTEGER NOT NULL DEFAULT 0,
                created_at        TEXT    NOT NULL,
                updated_at        TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS exchanges (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id     INTEGER NOT NULL,
                role        TEXT    NOT NULL,   -- 'user' | 'assistant'
                content     TEXT    NOT NULL,
                model       TEXT,
                created_at  TEXT    NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_exchanges_chat ON exchanges(chat_id, created_at);
        """)
        self._conn.commit()

    # ── Session access ──────────────────────────────────────────────────────

    def get_session(self, chat_id: int, default_model: str, tools_default: bool) -> dict:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE chat_id = ?", (chat_id,)
        ).fetchone()

        if row is None:
            now = _now()
            self._conn.execute(
                """INSERT INTO sessions
                   (chat_id, model, tools_enabled, claude_session_id, message_count, created_at, updated_at)
                   VALUES (?, ?, ?, NULL, 0, ?, ?)""",
                (chat_id, default_model, int(tools_default), now, now),
            )
            self._conn.commit()
            return {
                "chat_id": chat_id,
                "model": default_model,
                "tools_enabled": tools_default,
                "claude_session_id": None,
                "message_count": 0,
            }

        return {
            "chat_id": chat_id,
            "model": row["model"],
            "tools_enabled": bool(row["tools_enabled"]),
            "claude_session_id": row["claude_session_id"],
            "message_count": row["message_count"],
        }

    def set_claude_session_id(self, chat_id: int, session_id: str):
        """Store the Claude CLI session UUID after first message."""
        self._conn.execute(
            "UPDATE sessions SET claude_session_id = ?, updated_at = ? WHERE chat_id = ?",
            (session_id, _now(), chat_id),
        )
        self._conn.commit()

    def increment_message_count(self, chat_id: int):
        self._conn.execute(
            "UPDATE sessions SET message_count = message_count + 1, updated_at = ? WHERE chat_id = ?",
            (_now(), chat_id),
        )
        self._conn.commit()

    def set_model(self, chat_id: int, model: str):
        self._conn.execute(
            "UPDATE sessions SET model = ?, updated_at = ? WHERE chat_id = ?",
            (model, _now(), chat_id),
        )
        self._conn.commit()

    def set_tools(self, chat_id: int, enabled: bool):
        self._conn.execute(
            "UPDATE sessions SET tools_enabled = ?, updated_at = ? WHERE chat_id = ?",
            (int(enabled), _now(), chat_id),
        )
        self._conn.commit()

    def reset_session(self, chat_id: int):
        """
        Clear the Claude session ID — next message starts a fresh Claude session.
        Model and tools settings are preserved.
        """
        self._conn.execute(
            "UPDATE sessions SET claude_session_id = NULL, message_count = 0, updated_at = ? WHERE chat_id = ?",
            (_now(), chat_id),
        )
        self._conn.commit()

    # ── Audit log ───────────────────────────────────────────────────────────

    def log_exchange(self, chat_id: int, role: str, content: str, model: Optional[str] = None):
        self._conn.execute(
            """INSERT INTO exchanges (chat_id, role, content, model, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (chat_id, role, content, model, _now()),
        )
        self._conn.commit()

    def close(self):
        self._conn.close()
