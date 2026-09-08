from __future__ import annotations

import json
from typing import Any, Callable


ALLOWED_MOODS = {
    "Professional / Controlled",
    "Guarded / Hesitant",
    "Defensive / Cornered",
    "Reluctant / Defeated",
    "Annoyed / Dismissive",
}


def generate_response(context: dict[str, Any], generator_call: Callable[..., str]) -> str:
    value = json.loads(generator_call(context=context))
    mood = value["mood"]
    if mood not in ALLOWED_MOODS:
        raise ValueError("Generator returned an unknown mood.")
    return f"[MOOD:{mood}]\n{str(value['speech']).strip()}"
