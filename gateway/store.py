import os
import sqlite3
import time

DB_PATH = os.getenv("ABAX_DB_PATH", "/tmp/abax-metadata.db")


class SandboxStore:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """Create the sandboxes table if it does not exist."""
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sandboxes (
                    sandbox_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_active_at REAL NOT NULL
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def register(self, sandbox_id: str, user_id: str):
        """Register a newly created sandbox."""
        now = time.time()
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO sandboxes (sandbox_id, user_id, created_at, last_active_at) VALUES (?, ?, ?, ?)",
                (sandbox_id, user_id, now, now),
            )
            conn.commit()
        finally:
            conn.close()

    def record_activity(self, sandbox_id: str):
        """Update the last_active_at timestamp for a sandbox."""
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE sandboxes SET last_active_at = ? WHERE sandbox_id = ?",
                (time.time(), sandbox_id),
            )
            conn.commit()
        finally:
            conn.close()

    def unregister(self, sandbox_id: str):
        """Remove a sandbox record."""
        conn = self._connect()
        try:
            conn.execute("DELETE FROM sandboxes WHERE sandbox_id = ?", (sandbox_id,))
            conn.commit()
        finally:
            conn.close()

    def get_idle_sandboxes(self, max_idle_seconds: int) -> list[str]:
        """Return sandbox IDs that have been idle longer than max_idle_seconds."""
        cutoff = time.time() - max_idle_seconds
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT sandbox_id FROM sandboxes WHERE last_active_at < ?",
                (cutoff,),
            ).fetchall()
            return [row[0] for row in rows]
        finally:
            conn.close()

    def get_sandbox_meta(self, sandbox_id: str) -> dict | None:
        """Return metadata for a single sandbox, or None if not found."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT sandbox_id, user_id, created_at, last_active_at FROM sandboxes WHERE sandbox_id = ?",
                (sandbox_id,),
            ).fetchone()
            if row is None:
                return None
            return {
                "sandbox_id": row[0],
                "user_id": row[1],
                "created_at": row[2],
                "last_active_at": row[3],
            }
        finally:
            conn.close()

    def all_sandbox_ids(self) -> list[str]:
        """Return all registered sandbox IDs."""
        conn = self._connect()
        try:
            rows = conn.execute("SELECT sandbox_id FROM sandboxes").fetchall()
            return [row[0] for row in rows]
        finally:
            conn.close()


# Global singleton
store = SandboxStore()
