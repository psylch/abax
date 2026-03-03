"""Session/message persistence for agent layer.

Extracted from gateway/store.py during the Infra layer separation refactor.
This is a self-contained SQLite store that only handles session and message
data — it does NOT manage sandbox metadata (that stays in the gateway).
"""

import os
import sqlite3
import time
import uuid

DB_PATH = os.getenv("ABAX_DB_PATH", "/tmp/abax-metadata.db")


class SessionStore:
    """Standalone session and message store with its own SQLite connection."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """Create sessions and messages tables if they do not exist."""
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    title TEXT,
                    sandbox_id TEXT,
                    created_at REAL NOT NULL,
                    last_active_at REAL NOT NULL
                )
                """
            )
            # Migrate: add sandbox_id column if missing (existing DBs)
            try:
                conn.execute("ALTER TABLE sessions ADD COLUMN sandbox_id TEXT")
            except sqlite3.OperationalError:
                pass  # column already exists
            conn.execute(
                """
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
                """
            )
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    # --- Session methods ---

    def create_session(self, user_id: str, title: str | None = None) -> dict:
        """Create a new session and return its metadata."""
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
            "created_at": now,
            "last_active_at": now,
        }

    def get_session(self, session_id: str) -> dict | None:
        """Return session metadata or None if not found."""
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

    def list_sessions(self, user_id: str) -> list[dict]:
        """Return all sessions for a user, ordered by last_active_at desc."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT session_id, user_id, title, sandbox_id, created_at, last_active_at FROM sessions WHERE user_id = ? ORDER BY last_active_at DESC",
                (user_id,),
            ).fetchall()
            return [
                {
                    "session_id": r[0],
                    "user_id": r[1],
                    "title": r[2],
                    "sandbox_id": r[3],
                    "created_at": r[4],
                    "last_active_at": r[5],
                }
                for r in rows
            ]
        finally:
            conn.close()

    # --- Session-container binding ---

    def bind_session_container(self, session_id: str, sandbox_id: str):
        """Bind a session to a container."""
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE sessions SET sandbox_id = ? WHERE session_id = ?",
                (sandbox_id, session_id),
            )
            conn.commit()
        finally:
            conn.close()

    def get_session_container(self, session_id: str) -> str | None:
        """Return the sandbox_id bound to a session, or None."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT sandbox_id FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def clear_session_container(self, sandbox_id: str):
        """Clear container binding from all sessions referencing this sandbox."""
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE sessions SET sandbox_id = NULL WHERE sandbox_id = ?",
                (sandbox_id,),
            )
            conn.commit()
        finally:
            conn.close()

    # --- Message methods ---

    def save_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_calls: str | None = None,
        tool_results: str | None = None,
    ) -> dict:
        """Save a message and update session last_active_at. Returns message dict."""
        now = time.time()
        conn = self._connect()
        try:
            cursor = conn.execute(
                "INSERT INTO messages (session_id, role, content, tool_calls, tool_results, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, role, content, tool_calls, tool_results, now),
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
            "tool_results": tool_results,
            "created_at": now,
        }

    def load_history(self, session_id: str) -> list[dict]:
        """Return all messages for a session, ordered by creation time."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, session_id, role, content, tool_calls, tool_results, created_at FROM messages WHERE session_id = ? ORDER BY id ASC",
                (session_id,),
            ).fetchall()
            return [
                {
                    "id": r[0],
                    "session_id": r[1],
                    "role": r[2],
                    "content": r[3],
                    "tool_calls": r[4],
                    "tool_results": r[5],
                    "created_at": r[6],
                }
                for r in rows
            ]
        finally:
            conn.close()
