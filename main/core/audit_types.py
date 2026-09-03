from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class AuditRequest:
    entity_mentions: list[dict[str, Any]]
    requested_access: str | None
    requested_content: str | None
    issue_claims: list[dict[str, Any]]
    follow_active_context: bool
    small_talk: bool
    context_action: str = "follow"


@dataclass
class ResponseContext:
    response_mode: str
    requested_content: str | None
    records: list[dict[str, Any]]
    public_material: dict[str, Any]
    score_result: dict[str, Any]
    approved_material: dict[str, Any]
    clarification: str | None
