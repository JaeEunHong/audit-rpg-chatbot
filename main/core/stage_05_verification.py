from __future__ import annotations

from typing import Any



LOOKUP_ACTIONS = {"overview", "lookup"}


def check_filtered_request(
    filtered: dict[str, Any],
    requested_action: str | None,
    requested_concerns: list[str],
    available_concerns: list[str] | None = None,
) -> dict[str, Any]:
    """Classify a filtered request before any scoring is attempted."""
    if not any(filtered.get(kind) for kind in ("customers", "contracts", "assets", "vins")):
        return {
            "status": "not_found",
            "state": "missing_entity_and_issue" if not requested_concerns else "missing_entity",
            "missing": ["entity"],
            "clarification_type": "missing_entity",
            "options": [],
        }
    if requested_action in LOOKUP_ACTIONS:
        return {"status": "ready_for_lookup", "state": "lookup", "missing": [], "clarification_type": None, "options": []}
    if not requested_concerns:
        return {
            "status": "clarification",
            "state": "missing_issue",
            "missing": ["concern"],
            "clarification_type": "choose_concern",
            "options": available_concerns or [],
        }
    if requested_action == "explain":
        return {"status": "ready_for_scoring", "state": "ready_for_scoring", "missing": [], "clarification_type": None, "options": []}
    if requested_action == "assess":
        return {"status": "ready_for_scoring", "state": "ready_for_scoring", "missing": [], "clarification_type": None, "options": []}
    return {"status": "ready_for_lookup", "state": "lookup", "missing": [], "clarification_type": None, "options": []}
