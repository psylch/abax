"""Read user context files from host filesystem (Tier 1: zero-container chat)."""

import os
from pathlib import Path

PERSISTENT_ROOT = os.getenv("ABAX_PERSISTENT_ROOT", "/tmp/abax-data")


def read_user_context(user_id: str) -> dict[str, str]:
    """Read all .md files from {PERSISTENT_ROOT}/{user_id}/context/.

    Returns dict of filename -> content. Returns empty dict if directory
    does not exist or is empty.
    """
    context_dir = (Path(PERSISTENT_ROOT) / user_id / "context").resolve()
    if not str(context_dir).startswith(str(Path(PERSISTENT_ROOT).resolve())):
        return {}
    if not context_dir.is_dir():
        return {}
    result = {}
    for entry in sorted(context_dir.iterdir()):
        if entry.is_file() and entry.suffix == ".md":
            try:
                result[entry.name] = entry.read_text(encoding="utf-8")
            except OSError:
                continue
    return result
