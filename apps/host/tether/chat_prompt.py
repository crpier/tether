"""Agent-only context augmentation for interactive chat prompts."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

_LOCALTIME_PATH = Path("/etc/localtime")
_ZONEINFO_MARKER = "zoneinfo/"


def local_timezone_name(now: datetime) -> str:
    """Resolve the host's IANA zone, falling back to its numeric UTC offset."""
    env_zone = os.environ.get("TZ")
    if env_zone:
        return env_zone
    try:
        if _LOCALTIME_PATH.is_symlink():
            target = str(_LOCALTIME_PATH.readlink())
            index = target.rfind(_ZONEINFO_MARKER)
            if index != -1:
                return target[index + len(_ZONEINFO_MARKER) :]
    except OSError:
        pass
    return now.strftime("%z") or "UTC"


def prompt_with_time_context(
    content: str,
    *,
    now: datetime,
    timezone_name: str,
) -> str:
    """Prefix a clean user turn with private wall-clock context for pi."""
    note = (
        f"[Tether note — the current time is {now.isoformat(timespec='seconds')} "
        f"({timezone_name}). "
        'Resolve relative times like "in 3 minutes" or "tomorrow at 9am" '
        "against it. This note is system-generated; do not mention it.]"
    )
    return f"{note}\n\n{content}"


__all__ = ["local_timezone_name", "prompt_with_time_context"]
