from __future__ import annotations

from typing import Any, Callable

from audit_types import AuditRequest
from stage_03_request_parser import merge_pending_request, parse_conversation_request
from stage_04_entity_resolution import expand_conversation_references, filter_related_data
from stage_05_verification import verify_request
from stage_05_verification import check_filtered_request
from stage_06_scoring import score_entities
from conversation_state import ConversationState


ACKNOWLEDGEMENTS = {"ok", "okay", "thanks", "thank you", "bummer", "right", "i see", "got it"}


def run_conversation_turn(
    message: str,
    case_data: dict[str, Any],
    *,
    parser_call: Callable[..., str],
    latest_messages: list[dict[str, Any]],
    conversation_state: ConversationState | None = None,
    known_concern_names: list[str] | None = None,
    image_text: str | None = None,
    ledger: dict[str, Any] | None = None,
    team: str = "default",
) -> dict[str, Any]:
    """Parse one auditor turn and return filtered data or clarification."""
    state_memory = conversation_state or ConversationState()
    if message.strip().casefold().rstrip(".!?") in ACKNOWLEDGEMENTS:
        return {
            "status": "acknowledged",
            "state": "small_talk",
            "action": "small_talk",
            "evidence": None,
            "request": {"mentioned_entities": [], "requested_concerns": [], "requested_action": None},
            "scoring": None,
            "filtered_data": {},
            "conversation_state": state_memory,
        }
    parsed = parse_conversation_request(
        message,
        parser_call,
        latest_messages=latest_messages,
        active_context=state_memory.prompt_context(),
        pending_request=state_memory.pending_confirmation,
        known_concern_names=known_concern_names,
        image_text=image_text,
    )
    request = merge_pending_request(state_memory.pending_confirmation, parsed)
    request["starting_points"] = list(request.get("starting_points") or [])
    request["starting_points"].extend(
        {"type": item.get("type"), "id": item.get("id")}
        for item in parsed.get("mentioned_entities", [])
    )
    request["starting_points"].extend(expand_conversation_references(
        case_data, parsed.get("references", []), latest_messages
    ))
    if not request["starting_points"]:
        request["starting_points"] = list(state_memory.focus_entities)
    filtered = filter_related_data(case_data, request)
    state = check_filtered_request(
        filtered,
        request.get("requested_action"),
        request.get("requested_concerns", []),
        sorted(case_data.get("concern_catalog", {})),
    )
    contract_count = len(filtered.get("contracts", []))
    customer_count = len(filtered.get("customers", []))
    if state.get("status") == "ready_for_scoring" and (contract_count > 60 or customer_count > 10):
        state = {
            "status": "clarification",
            "state": "scope_too_large",
            "missing": ["smaller_scope"],
            "clarification_type": "scope_too_large",
            "options": [],
        }
    scoring = None
    if ledger is not None and state.get("state") == "ready_for_scoring":
        scoring = score_entities(
            case_data,
            ledger,
            request.get("starting_points", []),
            request["requested_concerns"][0],
            team=team,
        )
    if request.get("starting_points"):
        state_memory.focus_entities = request["starting_points"]
    if request.get("requested_concerns") or request.get("requested_action"):
        state_memory.focus_topic = {
            "action": request.get("requested_action"),
            "issue": (request.get("requested_concerns") or [None])[0],
        }
    state_memory.pending_confirmation = request if state["status"] == "clarification" else None
    return {
        **state,
        "request": request,
        "scoring": scoring,
        "filtered_data": filtered,
        "conversation_state": state_memory,
    }


def run_audit_pipeline(
    request: AuditRequest,
    case_data: dict[str, Any],
    ledger: dict[str, Any],
    *,
    visual_extractor: Callable[..., Any] | None = None,
    parser: Callable[..., AuditRequest] | None = None,
    parser_review: Callable[..., AuditRequest] | None = None,
    generator: Callable[..., str] | None = None,
    image: Any = None,
) -> dict[str, Any]:
    if image is not None and visual_extractor is not None:
        visual_extractor(image)
    if parser is not None:
        request = parser(request, parser_review)
    result = verify_request(request, case_data, ledger)
    if generator is not None:
        result["reply"] = generator(result)
    return result
