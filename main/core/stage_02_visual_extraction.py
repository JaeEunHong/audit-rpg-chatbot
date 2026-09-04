from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


def extract_visible_entities(image: Any, vision_call: Callable[..., str]) -> str:
    """Return the complete visible table; issue classification is not allowed here."""
    return vision_call(image=image)
