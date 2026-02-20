"""
JSONL session persistence for Abax Agent.

Each session is a JSONL file where each line is a message in Claude API format.
"""

import json
import uuid
from pathlib import Path

SESSIONS_DIR = Path(__file__).parent.parent / "sessions"


class Session:
    def __init__(self, session_id: str | None = None):
        self.id = session_id or uuid.uuid4().hex[:8]
        self.messages: list[dict] = []
        self.path = SESSIONS_DIR / f"{self.id}.jsonl"

    def add_message(self, role: str, content) -> None:
        """Add a message. Content can be str or list of content blocks."""
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        self.messages.append({"role": role, "content": content})

    def save(self) -> None:
        """Save all messages to JSONL file."""
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w") as f:
            for msg in self.messages:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")

    @classmethod
    def load(cls, session_id: str) -> "Session":
        """Load session from JSONL file."""
        session = cls(session_id)
        if session.path.exists():
            with open(session.path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        session.messages.append(json.loads(line))
        return session

    @classmethod
    def list_sessions(cls) -> list[dict]:
        """List all sessions with basic info."""
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        sessions = []
        for p in sorted(SESSIONS_DIR.glob("*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True):
            sid = p.stem
            # Read first user message as preview
            preview = ""
            with open(p) as f:
                for line in f:
                    msg = json.loads(line.strip())
                    if msg["role"] == "user":
                        content = msg["content"]
                        if isinstance(content, str):
                            preview = content[:50]
                        elif isinstance(content, list):
                            for block in content:
                                if block.get("type") == "text":
                                    preview = block["text"][:50]
                                    break
                        break
            sessions.append({"id": sid, "preview": preview})
        return sessions
