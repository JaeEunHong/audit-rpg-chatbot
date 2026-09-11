from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ResolvedRequest:
    """The normalized request after parsing and conversation-context merging."""

    entities: list[dict[str, Any]]
    action: str | None
    concerns: list[str]
    issue_candidates: list[dict[str, Any]]
    scope_intent: str = "unspecified_related_records"
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
    score_eligible: bool = False


@dataclass
class EvidencePackage:
    """Structured input for response generation; Python does not summarize narrative."""

    status: str
    issue: str | None
    selected_count: int
    confirmed_count: int
    unsupported_count: int
    score_delta: int
    score_eligible: bool
    entity_samples: list[dict[str, Any]]
    public_narrative_samples: list[dict[str, Any]]
    secret_narrative_samples: list[dict[str, Any]]
    tone: str | None = None
