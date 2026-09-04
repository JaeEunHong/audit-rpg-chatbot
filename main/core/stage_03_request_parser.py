from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any, Callable

from audit_types import AuditRequest
from stage_01_case_data import normalize_compact_id


REQUEST_TYPES = {"new", "continue"}
REQUEST_ACTIONS = {"overview", "lookup", "explain", "assess", "compare"}


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
    parser_payload = dict(
        current_message={"speaker": "auditor", "content": message},
        latest_messages=latest_messages[-10:],
        active_context=active_context or {},
        pending_request=pending_request or {},
        known_concern_names=known_concern_names or [],
        image_text=image_text,
    )
    value = json.loads(parser_call(**parser_payload))
    expected_ids = set(re.findall(
        r"\b(?:SE\s*\d{6}|CUST\s*\d{1,4}|AST\s*\d{6}|[A-HJ-NPR-Z0-9]{17})\b",
        image_text or "",
        re.IGNORECASE,
    ))
    returned_ids = {
        str(item.get("id") or "").replace(" ", "").upper()
        for item in value.get("entities", value.get("mentioned_entities", []))
    }
    normalized_expected = {item.replace(" ", "").upper() for item in expected_ids}
    if normalized_expected - returned_ids:
        parser_payload["retry_instruction"] = (
            "The attached Markdown contains visible entity IDs that are missing "
            "from entities. Return every visible customer, contract, asset, and "
            "VIN ID. Keep the issue and request unchanged."
        )
        value = json.loads(parser_call(**parser_payload))
    mentioned_entities = []
    seen_entities: set[tuple[str, str]] = set()
    raw_entities = value.get("entities")
    if raw_entities is None:
        raw_entities = value.get("mentioned_entities")
    for item in list(raw_entities or []):
        kind = str(item.get("type") or "").lower()
        entity_id = normalize_compact_id(item.get("id") or "")
        if kind == "customer":
            entity_id = "CUST" + entity_id[4:].zfill(4) if entity_id.startswith("CUST") else entity_id
        elif kind == "contract":
            entity_id = "SE" + entity_id[2:] if entity_id.startswith("SE") else entity_id
        elif kind == "asset":
            entity_id = "AST" + entity_id[3:] if entity_id.startswith("AST") else entity_id
        key = (kind, entity_id)
        if kind and entity_id and key not in seen_entities:
            mentioned_entities.append({"type": kind, "id": entity_id})
            seen_entities.add(key)
    concerns = []
    raw_concerns = value.get("requested_concerns")
    if raw_concerns is None:
        raw_concerns = [value["issue"]] if value.get("issue") else []
    for concern in list(raw_concerns or []):
        normalized = str(concern).strip().upper()
        if normalized and normalized not in concerns:
            concerns.append(normalized)
    action = value.get("requested_action")
    if action is None:
        action = {"check": "assess", "unknown": None}.get(value.get("request"), value.get("request"))
    request_type = str(value.get("request_type") or "")
    if not request_type and pending_request and not mentioned_entities and not value.get("references"):
        request_type = "continue"
    parsed = {
        "request_type": request_type or "new",
        "mentioned_entities": mentioned_entities,
        "references": list(value.get("references") or []),
        "requested_concerns": concerns,
        "requested_details": list(value.get("requested_details") or []),
        "requested_action": action,
        "filled_values": dict(value.get("filled_values") or {}),
        "needs_clarification": bool(value.get("needs_clarification")),
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


def parse_request(message: str, parser_call: Callable[..., str], **context: Any) -> AuditRequest:
    value = json.loads(parser_call(message=message, **context))
    return AuditRequest(
        entity_mentions=list(value.get("entity_mentions") or []),
        requested_access=value.get("requested_access"),
        requested_content=value.get("requested_content"),
        issue_claims=list(value.get("issue_claims") or []),
        follow_active_context=bool(value.get("follow_active_context")),
        small_talk=bool(value.get("small_talk")),
        context_action=str(value.get("context_action") or "follow"),
    )


def parse_request_with_review(
    message: str,
    parser_call: Callable[..., str],
    review_call: Callable[..., str] | None = None,
    **context: Any,
) -> AuditRequest:
    request = parse_request(message, parser_call, **context)
    needs_review = (
        review_call is not None
        and request.requested_content in {"explanation", "policy"}
        and (not request.entity_mentions or not request.issue_claims)
    )
    if needs_review:
        request = parse_request(message, review_call, draft=request.__dict__, **context)
    return request
