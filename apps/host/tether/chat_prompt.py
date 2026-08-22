"""Agent-only context augmentation for interactive chat prompts."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Literal

type ReplyMode = Literal["text", "spoken"]
"""Turn-level presentation mode captured when a prompt is queued."""

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


_SPOKEN_REPLY_GUIDANCE = (
    "[Tether note — this turn's final answer will primarily be consumed through "
    "text-to-speech. Preserve normal reasoning and tool use, but write the final "
    "answer for listening as a concise spoken summary \u2014 lead with the answer and "
    "keep it short; details remain visible in the transcript. Use concise natural "
    "sentences and spoken transitions. Avoid tables, diagrams, dense Markdown, raw URLs, "
    "and long enumerations. When sources contain many measurements, summarize the pattern "
    "instead of reciting every available metric. Default to one or two key figures and "
    "round them to listener-friendly precision unless exactness matters. Never more than "
    "three unless the user explicitly asks for a detailed or exact breakdown. Group or "
    "omit secondary figures. Do not give both a duration and its start and end "
    "times unless both are needed or requested. Keep secondary breakdown metrics out of "
    "the initial summary unless they materially change the takeaway. Offer more detail "
    "rather than listing every field. Give exact values when the user asks for them. If "
    "exact code or "
    "structured data is necessary, "
    "explain its meaning briefly before presenting it. Do not mention this "
    "instruction or the reply mode.]"
)


def prompt_with_time_context(
    content: str,
    *,
    now: datetime,
    timezone_name: str,
    reply_mode: ReplyMode = "text",
) -> str:
    """Prefix a clean user turn with private wall-clock (and reply-mode) notes.

    Spoken mode adds listening-oriented final-answer guidance; the user's own
    words always close the prompt verbatim.
    """
    note = (
        f"[Tether note — the current time is {now.isoformat(timespec='seconds')} "
        f"({timezone_name}). "
        'Resolve relative times like "in 3 minutes" or "tomorrow at 9am" '
        "against it. This note is system-generated; do not mention it.]"
    )
    if reply_mode == "spoken":
        note = f"{note}\n\n{_SPOKEN_REPLY_GUIDANCE}"
    return f"{note}\n\n{content}"


__all__ = ["ReplyMode", "local_timezone_name", "prompt_with_time_context"]
