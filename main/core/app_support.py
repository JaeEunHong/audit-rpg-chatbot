from __future__ import annotations

import base64
import os
import re
from pathlib import Path
from typing import Any


DEFAULT_MODEL = "gpt-4.1-mini"
MOOD_RE = re.compile(r"^\[MOOD:([^\]]+)\]\s*", re.IGNORECASE)
MOODS = {
    "Professional / Controlled",
    "Guarded / Hesitant",
    "Defensive / Cornered",
    "Reluctant / Defeated",
    "Annoyed / Dismissive",
}
INTERVIEW_STATE_BY_MOOD = {
    "Professional / Controlled": {"mood": "Confident"},
    "Guarded / Hesitant": {"mood": "Hesitant"},
    "Defensive / Cornered": {"mood": "Defensive"},
    "Reluctant / Defeated": {"mood": "Reluctant"},
    "Annoyed / Dismissive": {"mood": "Dismissive"},
}


def load_env(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def extract_mood(reply: str) -> str:
    match = MOOD_RE.match(reply or "")
    return match.group(1) if match and match.group(1) in MOODS else "Professional / Controlled"


def interview_state_for_mood(mood: str) -> dict[str, str]:
    return INTERVIEW_STATE_BY_MOOD.get(mood, INTERVIEW_STATE_BY_MOOD["Professional / Controlled"])


def image_to_data_url(file_name: str, content: bytes) -> str:
    extension = Path(file_name).suffix.lower().lstrip(".") or "png"
    mime = "image/jpeg" if extension in {"jpg", "jpeg"} else "image/png"
    return f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}"


def small_talk_reply(chat_history: list[dict[str, Any]], _case_data: dict[str, Any] | None = None) -> str | None:
    latest = next((str(item.get("content") or "").strip().lower() for item in reversed(chat_history) if item.get("role") == "user"), "")
    if latest in {"hi", "hello", "hey", "hi there", "good morning", "morning"}:
        return "[MOOD:Professional / Controlled]\nMorning. We can discuss the book, but start me with a contract or customer."
    if re.fullmatch(r"how\s+(are|r)\s+(you|u)\??", latest):
        return "[MOOD:Annoyed / Dismissive]\nBusy, as usual. If we're doing this, start with a contract or customer."
    return None
