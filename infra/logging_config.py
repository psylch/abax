"""Structured JSON logging configuration for Abax gateway.

Uses Python's built-in logging with a custom JSON formatter.
Adds request_id and sandbox_id context via contextvars.
"""

import json
import logging
import sys
from contextvars import ContextVar

# Context variables for request-scoped fields
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
sandbox_id_var: ContextVar[str | None] = ContextVar("sandbox_id", default=None)


class JSONFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Attach context vars if set
        req_id = request_id_var.get()
        if req_id is not None:
            log_entry["request_id"] = req_id

        sb_id = sandbox_id_var.get()
        if sb_id is not None:
            log_entry["sandbox_id"] = sb_id

        # Include exception info if present
        if record.exc_info and record.exc_info[0] is not None:
            log_entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_entry, default=str)


def setup_logging(level: str = "INFO") -> None:
    """Configure root logger with structured JSON output."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # Remove any existing handlers to avoid duplicate output
    root.handlers.clear()
    root.addHandler(handler)
