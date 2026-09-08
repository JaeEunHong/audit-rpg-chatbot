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
    scope_entities: list[dict[str, str]] = field(default_factory=list)
    scope_start: int = 0
    scope_end: int = 0
    attitude: dict[str, Any] = field(default_factory=lambda: {
        "pressure": 0,
        "stage": "confident",
        "last_trigger": None,
        "last_delta": 0,
    })
    response_tone: str = "confident"
    has_scored_finding: bool = False

    def prompt_context(self) -> dict[str, Any]:
        entity_types = {}
        for entity in self.focus_entities:
            kind = str(entity.get("type") or "unknown")
            entity_types[kind] = entity_types.get(kind, 0) + 1
        scope_types = {}
        for entity in self.scope_entities:
            kind = str(entity.get("type") or "unknown")
            scope_types[kind] = scope_types.get(kind, 0) + 1
        return {
            "focus_summary": {
                "entity_types": entity_types,
                "entity_count": len(self.focus_entities),
                "issue": (self.focus_topic or {}).get("issue"),
            },
            "pending_summary": {
                "issue": (self.pending_confirmation or {}).get("requested_concerns", []),
                "action": (self.pending_confirmation or {}).get("requested_action"),
                "missing": (self.pending_confirmation or {}).get("missing", []),
            },
            "scope_summary": {
                "entity_types": scope_types,
                "entity_count": len(self.scope_entities),
                "start": self.scope_start,
                "end": self.scope_end,
            },
            "scope_start": self.scope_start,
            "scope_end": self.scope_end,
            "attitude": self.attitude,
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
            scope_entities=list(value.get("scope_entities") or []),
            scope_start=int(value.get("scope_start") or 0),
            scope_end=int(value.get("scope_end") or 0),
            attitude=dict(value.get("attitude") or {
                "pressure": 0,
                "stage": "confident",
                "last_trigger": None,
                "last_delta": 0,
            }),
            response_tone=str(value.get("response_tone") or "confident"),
            has_scored_finding=bool(value.get("has_scored_finding", False)),
        )
