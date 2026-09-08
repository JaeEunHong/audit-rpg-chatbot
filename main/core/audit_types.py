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
    clarification_kind: str | None = None
    clarification_candidates: list[str] | None = None


@dataclass
class ResolvedRequest:
    """The normalized request after parsing and conversation-context merging."""

    entities: list[dict[str, Any]]
    action: str | None
    concerns: list[str]
    issue_candidates: list[dict[str, Any]]
    selection: dict[str, Any] | None = None
    continuation: bool = False
    clarification: str | None = None


@dataclass
class SelectedData:
    """Graph data selected for one request; narrative remains raw text."""

    customers: list[Any]
    contracts: list[Any]
    assets: list[Any]
    vins: list[Any]
    customer_concerns: list[dict[str, Any]]
    contract_concerns: list[dict[str, Any]]


@dataclass
class DecisionResult:
    """Verification and scoring output for the selected data."""

    status: str
    findings: list[dict[str, Any]]
    score_delta: int = 0
    confirmed_count: int = 0
    unsupported_count: int = 0
    repeat_count: int = 0


@dataclass
class EvidencePackage:
    """Structured input for response generation; Python does not summarize narrative."""

    status: str
    issue: str | None
    selected_count: int
    confirmed_count: int
    unsupported_count: int
    score_delta: int
    entity_samples: list[dict[str, Any]]
    public_narrative_samples: list[dict[str, Any]]
    secret_narrative_samples: list[dict[str, Any]]
    tone: str | None = None
