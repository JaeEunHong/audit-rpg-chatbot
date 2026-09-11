from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from typing import Any, Callable

from audit_types import ResolvedRequest
from stage_01_case_data import normalize_compact_id


REQUEST_TYPES = {"new", "continue"}
REQUEST_ACTIONS = {"overview", "lookup", "explain", "assess", "compare", "small_talk"}


def _parse_json_response(text: str, stage: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        if os.getenv("AUDIT_DEBUG_JSON") == "1":
            print(f"[JSON_DEBUG] stage={stage} length={len(text)}")
            print(f"[JSON_DEBUG] position={exc.pos} tail={text[-500:]!r}")
            print(f"[JSON_DEBUG] context={text[max(0, exc.pos - 150):exc.pos + 150]!r}")
        raise


def resolved_request_from_dict(request: dict[str, Any]) -> ResolvedRequest:
    """Create the Phase A value object from the compatibility request dict."""
    return ResolvedRequest(
        entities=list(request.get("starting_points") or request.get("mentioned_entities") or []),
        action=request.get("requested_action"),
        concerns=list(request.get("requested_concerns") or []),
        issue_candidates=list(request.get("issue_candidates") or []),
        scope_intent=request.get("scope_intent") or "unspecified_related_records",
        selection=request.get("selection"),
        continuation=request.get("request_type") == "continue",
        clarification=(request.get("missing") or [None])[0],
    )


def extract_explicit_entities(text: str) -> list[dict[str, str]]:
    patterns = (
        ("customer", r"\bCUST\s*\d{1,4}\b"),
        ("contract", r"\bSE\s*\d{6}\b"),
        ("asset", r"\bAST\s*\d{6}\b"),
        ("vin", r"\b[A-HJ-NPR-Z0-9]{17}\b"),
    )
    entities = []
    seen = set()
    for kind, pattern in patterns:
        for match in re.finditer(pattern, str(text or ""), re.IGNORECASE):
            entity_id = normalize_compact_id(match.group(0))
            if (kind, entity_id) not in seen:
                entities.append({"type": kind, "id": entity_id})
                seen.add((kind, entity_id))
    return entities


def extract_identifier_like_tokens(text: str) -> list[str]:
    """Find identifier-shaped tokens that Python must not silently ignore."""
    tokens = []
    seen = set()
    for match in re.finditer(r"\b[A-Z]{2,6}\s*\d{4,}\b", str(text or ""), re.IGNORECASE):
        token = re.sub(r"\s+", "", match.group(0)).upper()
        if token not in seen:
            tokens.append(token)
            seen.add(token)
    return tokens


def parse_conversation_request(
    message: str,
    parser_call: Callable[..., str],
    *,
    latest_messages: list[dict[str, Any]],
    active_context: dict[str, Any] | None = None,
    pending_request: dict[str, Any] | None = None,
    known_concern_names: list[Any] | None = None,
    image_text: str | None = None,
) -> dict[str, Any]:
    """Parse an auditor message with bounded conversation context."""
    explicit_entities = extract_explicit_entities(message)
    for entity in extract_explicit_entities(image_text or ""):
        if entity not in explicit_entities:
            explicit_entities.append(entity)
    identifier_tokens = extract_identifier_like_tokens(message)
    identifier_tokens.extend(extract_identifier_like_tokens(image_text or ""))
    known_entity_ids = {item["id"] for item in explicit_entities}
    unresolved_entity_mentions = []
    for token in identifier_tokens:
        if token not in known_entity_ids and token not in unresolved_entity_mentions:
            unresolved_entity_mentions.append(token)
    entity_counts: dict[str, int] = {}
    for entity in explicit_entities:
        entity_type = entity["type"]
        entity_counts[entity_type] = entity_counts.get(entity_type, 0) + 1
    parser_payload = dict(
        current_message={"speaker": "auditor", "content": message},
        explicit_entity_summary={
            "counts": entity_counts,
            "sample": explicit_entities[:5],
        },
        latest_messages=latest_messages[-3:],
        active_context=active_context or {},
        pending_request=pending_request or {},
        known_concern_names=known_concern_names or [],
        image_text=image_text,
    )
    value = _parse_json_response(parser_call(**parser_payload), "llm1")
    # Entity resolution is deliberately owned by Python. LLM1 only classifies
    # the concern and requested action; it must not emit entity-like fields.
    mentioned_entities = explicit_entities
    concerns = []
    raw_concerns = value.get("requested_concerns")
    if raw_concerns is None:
        raw_concerns = value.get("issues") or ([value["issue"]] if value.get("issue") else [])
    if value.get("issue") and value["issue"] not in raw_concerns:
        raw_concerns = [*raw_concerns, value["issue"]]
    for concern in list(raw_concerns or []):
        normalized = str(concern).strip().upper()
        if normalized and normalized not in concerns:
            concerns.append(normalized)
    action = value.get("requested_action")
    if action is None:
        action = {"check": "assess", "unknown": None}.get(value.get("request"), value.get("request"))
    request_type = str(value.get("request_type") or "")
    if not request_type and pending_request and not mentioned_entities:
        request_type = "continue"
    parsed = {
        "request_type": request_type or "new",
        "mentioned_entities": mentioned_entities,
        "unresolved_entity_mentions": unresolved_entity_mentions,
        "references": [],
        "selection": None,
        "requested_concerns": concerns,
        "requested_details": list(value.get("requested_details") or []),
        "requested_action": action,
        "response_mode": str(value.get("response_mode") or "standard"),
        "filled_values": dict(value.get("filled_values") or {}),
        "needs_clarification": bool(value.get("needs_clarification")),
        "small_talk": value.get("request") == "small_talk",
        "issue_candidates": list(value.get("issue_candidates") or []),
        "scope_intent": str(value.get("scope_intent") or "unspecified_related_records"),
        "needs_issue_clarification": bool(value.get("needs_issue_clarification")),
    }
    if parsed["request_type"] == "continue":
        if concerns:
            parsed["filled_values"]["concern"] = concerns[0]
        if action:
            parsed["filled_values"]["action"] = action
    if parsed["request_type"] not in REQUEST_TYPES:
        parsed["request_type"] = "new"
    if parsed["requested_action"] not in REQUEST_ACTIONS:
        parsed["requested_action"] = None
    return parsed


def merge_pending_request(
    pending_request: dict[str, Any] | None,
    parsed_request: dict[str, Any],
) -> dict[str, Any]:
    """Apply a continuation's filled values to the pending request."""
    if not pending_request or parsed_request.get("request_type") == "new":
        return deepcopy(parsed_request)
    merged = deepcopy(pending_request)
    fills = parsed_request.get("filled_values", {})
    if fills.get("concern"):
        merged["requested_concerns"] = [fills["concern"]]
    if fills.get("action"):
        merged["requested_action"] = fills["action"]
    if fills.get("starting_point"):
        merged["starting_points"] = [fills["starting_point"]]
    merged["missing"] = [
        item for item in merged.get("missing", [])
        if item not in fills
    ]
    merged["needs_clarification"] = bool(merged.get("missing"))
    return merged
