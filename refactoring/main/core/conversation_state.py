from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ConversationState:
    """The small, persistent memory used between auditor turns."""

    focus_entities: list[dict[str, str]] = field(default_factory=list)
    focus_topic: dict[str, Any] | None = None
    last_raw_data: dict[str, Any] | None = None
    pending_confirmation: dict[str, Any] | None = None
    recent_turns: list[dict[str, str]] = field(default_factory=list)

    def prompt_context(self) -> dict[str, Any]:
        return {
            "focus_entities": self.focus_entities,
            "focus_topic": self.focus_topic,
            "last_raw_data": self.last_raw_data,
            "pending_confirmation": self.pending_confirmation,
        }

    def replace_focus(self, entities: list[dict[str, str]], topic: dict[str, Any] | None) -> None:
        self.focus_entities = entities
        self.focus_topic = topic

    def add_turn(self, auditor: str, mikael: str) -> None:
        self.recent_turns.append({"auditor": auditor, "mikael": mikael})
        self.recent_turns = self.recent_turns[-3:]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "ConversationState":
        value = value or {}
        return cls(
            focus_entities=list(value.get("focus_entities") or value.get("entities") or []),
            focus_topic=value.get("focus_topic"),
            last_raw_data=value.get("last_raw_data"),
            pending_confirmation=value.get("pending_confirmation") or value.get("pending_request"),
            recent_turns=list(value.get("recent_turns") or []),
        )
