"""Session and message persistence — async wrapper over SQLite."""

import asyncio
import json
import os
import sqlite3
import time
import uuid

DB_PATH = os.getenv("ABAX_AGENT_DB_PATH", "/tmp/abax-agent.db")


class SessionStore:
    """SQLite store for sessions and messages.

    Synchronous SQLite methods wrapped with asyncio.to_thread() for
    non-blocking usage in async routes. Each call creates its own
    connection for thread safety.
    """

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    title TEXT,
                    sandbox_id TEXT,
                    created_at REAL NOT NULL,
                    last_active_at REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    tool_calls TEXT,
                    tool_results TEXT,
                    created_at REAL NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
                )
            """)
            conn.commit()
        finally:
            conn.close()

    # --- Sync internals ---

    def _create_session(self, user_id: str, title: str | None = None) -> dict:
        session_id = uuid.uuid4().hex
        now = time.time()
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO sessions (session_id, user_id, title, created_at, last_active_at) VALUES (?, ?, ?, ?, ?)",
                (session_id, user_id, title, now, now),
            )
            conn.commit()
        finally:
            conn.close()
        return {
            "session_id": session_id,
            "user_id": user_id,
            "title": title,
            "sandbox_id": None,
            "created_at": now,
            "last_active_at": now,
        }

    def _get_session(self, session_id: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT session_id, user_id, title, sandbox_id, created_at, last_active_at FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            return {
                "session_id": row[0],
                "user_id": row[1],
                "title": row[2],
                "sandbox_id": row[3],
                "created_at": row[4],
                "last_active_at": row[5],
            }
        finally:
            conn.close()

    def _list_sessions(self, user_id: str) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT session_id, user_id, title, sandbox_id, created_at, last_active_at FROM sessions WHERE user_id = ? ORDER BY last_active_at DESC",
                (user_id,),
            ).fetchall()
            return [
                {
                    "session_id": r[0], "user_id": r[1], "title": r[2],
                    "sandbox_id": r[3], "created_at": r[4], "last_active_at": r[5],
                }
                for r in rows
            ]
        finally:
            conn.close()

    def _delete_session(self, session_id: str) -> bool:
        conn = self._connect()
        try:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            cursor = conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def _bind_sandbox(self, session_id: str, sandbox_id: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE sessions SET sandbox_id = ? WHERE session_id = ?",
                (sandbox_id, session_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _save_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_calls: list[dict] | None = None,
        tool_results: list[dict] | None = None,
    ) -> dict:
        now = time.time()
        tc_json = json.dumps(tool_calls) if tool_calls else None
        tr_json = json.dumps(tool_results) if tool_results else None
        conn = self._connect()
        try:
            cursor = conn.execute(
                "INSERT INTO messages (session_id, role, content, tool_calls, tool_results, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, role, content, tc_json, tr_json, now),
            )
            msg_id = cursor.lastrowid
            conn.execute(
                "UPDATE sessions SET last_active_at = ? WHERE session_id = ?",
                (now, session_id),
            )
            conn.commit()
        finally:
            conn.close()
        return {
            "id": msg_id,
            "session_id": session_id,
            "role": role,
            "content": content,
            "tool_calls": tool_calls,
            "created_at": now,
        }

    def _load_history(self, session_id: str) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, session_id, role, content, tool_calls, tool_results, created_at FROM messages WHERE session_id = ? ORDER BY id ASC",
                (session_id,),
            ).fetchall()
            return [
                {
                    "id": r[0], "session_id": r[1], "role": r[2], "content": r[3],
                    "tool_calls": json.loads(r[4]) if r[4] else None,
                    "tool_results": json.loads(r[5]) if r[5] else None,
                    "created_at": r[6],
                }
                for r in rows
            ]
        finally:
            conn.close()

    # --- Async wrappers ---

    async def create_session(self, user_id: str, title: str | None = None) -> dict:
        return await asyncio.to_thread(self._create_session, user_id, title)

    async def get_session(self, session_id: str) -> dict | None:
        return await asyncio.to_thread(self._get_session, session_id)

    async def list_sessions(self, user_id: str) -> list[dict]:
        return await asyncio.to_thread(self._list_sessions, user_id)

    async def delete_session(self, session_id: str) -> bool:
        return await asyncio.to_thread(self._delete_session, session_id)

    async def bind_sandbox(self, session_id: str, sandbox_id: str) -> None:
        await asyncio.to_thread(self._bind_sandbox, session_id, sandbox_id)

    async def save_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_calls: list[dict] | None = None,
        tool_results: list[dict] | None = None,
    ) -> dict:
        return await asyncio.to_thread(
            self._save_message, session_id, role, content, tool_calls, tool_results
        )

    async def load_history(self, session_id: str) -> list[dict]:
        return await asyncio.to_thread(self._load_history, session_id)


# Module-level singleton
store = SessionStore()
