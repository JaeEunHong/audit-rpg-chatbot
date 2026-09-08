from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable

from audit_types import AuditRequest
from stage_03_request_parser import merge_pending_request, parse_conversation_request
from stage_04_entity_resolution import expand_conversation_references, filter_related_data, resolve_target
from stage_05_verification import verify_request
from stage_05_verification import check_filtered_request
from stage_06_scoring import score_entities
from conversation_state import ConversationState


ACKNOWLEDGEMENTS = {"ok", "okay", "thanks", "thank you", "bummer", "right", "i see", "got it"}


def _explicit_asset_double_financing(message: str) -> bool:
    text = str(message or "").casefold()
    return (
        ("financ" in text and "twice" in text)
        or "double financing" in text
        or "financed more than once" in text
        or ("same vin" in text and "different contract" in text)
        or (
            "vin" in text
            and ("identical" in text or "same" in text)
            and "different contract" in text
        )
    )


def _attitude_stage(pressure: int) -> str:
    if pressure <= 50:
        return "confident"
    if pressure <= 150:
        return "guarded"
    if pressure <= 400:
        return "defensive"
    if pressure <= 900:
        return "nervous"
    return "defeated"


def _update_attitude(state: ConversationState, scoring: dict[str, Any] | None) -> dict[str, Any]:
    before = dict(state.attitude)
    if not scoring:
        return {"before": before, "after": before, "trigger": None, "pressure_delta": 0}
    findings = list(scoring.get("findings") or [])
    new_findings = [item for item in findings if item.get("status") == "new_score"]
    unsupported_findings = [item for item in findings if item.get("status") == "unsupported"]
    new_contracts = {
        str(item.get("contract_id") or item.get("record_id") or "").strip()
        for item in new_findings
        if str(item.get("contract_id") or item.get("record_id") or "").strip()
    }
    new_count = len(new_contracts)
    unsupported_records = {
        str(item.get("contract_id") or item.get("customer_id") or item.get("record_id") or "").strip()
        for item in unsupported_findings
        if str(item.get("contract_id") or item.get("customer_id") or item.get("record_id") or "").strip()
    }
    unsupported_count = len(unsupported_records)
    issue_types = {
        str(item.get("issue_type") or item.get("issue_key") or "").strip().upper()
        for item in new_findings
        if str(item.get("issue_type") or item.get("issue_key") or "").strip()
    }
    diversity_multiplier = min(3.0, 1.0 + 0.5 * max(0, len(issue_types) - 1))
    if new_count:
        trigger = "new_score"
        pressure_delta = round(new_count * diversity_multiplier)
    elif unsupported_count:
        trigger = "unsupported"
        unsupported_issue_types = {
            str(item.get("issue_type") or item.get("issue_key") or "").strip().upper()
            for item in unsupported_findings
            if str(item.get("issue_type") or item.get("issue_key") or "").strip()
        }
        pressure_delta = -min(10, max(1, unsupported_count))
    elif any(item.get("status") == "repeat" for item in findings):
        trigger = "repeat"
        pressure_delta = 0
    else:
        trigger = None
        pressure_delta = 0
    pressure = max(0, int(before.get("pressure", 0)) + pressure_delta)
    after = {
        "pressure": pressure,
        "stage": _attitude_stage(pressure),
        "last_trigger": trigger,
        "last_delta": pressure_delta,
    }
    state.attitude = after
    return {
        "before": before,
        "after": after,
        "trigger": trigger,
        "pressure_delta": pressure_delta,
        "new_finding_count": len(new_findings),
        "new_contract_count": new_count,
        "new_issue_type_count": len(issue_types),
        "diversity_multiplier": diversity_multiplier if new_count else 1.0,
    }


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
    pending_for_parser = state_memory.pending_confirmation or {}
    pending_summary = {
        key: pending_for_parser.get(key)
        for key in ("request_type", "requested_concerns", "requested_action", "missing", "filled_values")
        if pending_for_parser.get(key) is not None
    }
    if False and message.strip().casefold().rstrip(".!?") in ACKNOWLEDGEMENTS:
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
        pending_request=pending_summary,
        known_concern_names=known_concern_names,
        image_text=image_text,
    )
    if parsed.get("small_talk"):
        return {
            "status": "small_talk",
            "state": "small_talk",
            "action": "small_talk",
            "request": {
                "mentioned_entities": [],
                "requested_concerns": [],
                "requested_action": "small_talk",
                "small_talk": True,
            },
            "scoring": None,
            "filtered_data": {},
            "conversation_state": state_memory,
        }
    # Keep an explicit double-financing claim from being hijacked by the
    # previous turn's issue when the conversation contains a large scope.
    if _explicit_asset_double_financing(message):
        parsed["requested_concerns"] = ["ASSET FINANCED TWICE"]
        parsed["requested_action"] = "assess"
        parsed["needs_issue_clarification"] = False
    candidates = [
        item for item in parsed.get("issue_candidates", [])
        if isinstance(item, dict) and str(item.get("description") or "").strip()
    ]
    if parsed.get("needs_issue_clarification") and len(candidates) >= 2:
        return {
            "status": "clarification",
            "state": "ambiguous_issue",
            "missing": ["single_issue"],
            "clarification_type": "ambiguous_issue",
            "issue_candidates": candidates[:3],
            "request": parsed,
            "scoring": None,
            "filtered_data": {},
            "conversation_state": state_memory,
        }
    pending = state_memory.pending_confirmation or {}
    pending_issues = list(pending.get("requested_concerns") or [])
    if not pending_issues:
        focus_issue = (state_memory.focus_topic or {}).get("issue")
        if focus_issue:
            pending_issues = [focus_issue]
    continuation_text = message.casefold()
    is_issue_continuation = any(
        phrase in continuation_text
        for phrase in ("same issue", "same concern", "more for this", "more on this")
    )
    if is_issue_continuation and pending_issues:
        parsed["requested_concerns"] = pending_issues
    if (
        pending_issues
        and parsed.get("mentioned_entities")
        and parsed.get("requested_action") in {"overview", "lookup", None}
    ):
        parsed["requested_concerns"] = pending_issues
        parsed["requested_action"] = pending.get("requested_action")
    request = merge_pending_request(state_memory.pending_confirmation, parsed)
    request["starting_points"] = list(request.get("starting_points") or [])
    request["starting_points"].extend(
        {"type": item.get("type"), "id": item.get("id")}
        for item in parsed.get("mentioned_entities", [])
    )
    if not parsed.get("mentioned_entities"):
        request["starting_points"].extend(expand_conversation_references(
            case_data, parsed.get("references", []), latest_messages
        ))
    if not request["starting_points"]:
        request["starting_points"] = list(state_memory.focus_entities)
    canonical_points = []
    for point in request["starting_points"]:
        target = resolve_target(case_data, point.get("type", ""), point.get("id", ""))
        if target.get("status") != "resolved":
            continue
        if point.get("type") == "customer":
            canonical_points.append({"type": "customer", "id": target["customer_id"]})
        elif point.get("type") == "contract":
            canonical_points.append({"type": "contract", "id": target["contract_id"]})
        elif point.get("type") in {"asset", "vin"}:
            canonical_points.extend(
                {"type": "contract", "id": contract_id}
                for contract_id in target.get("contract_ids", [])
            )
        else:
            canonical_points.append(point)
    if canonical_points:
        request["starting_points"] = canonical_points
    selection = request.get("selection")
    if selection:
        mode = str(selection.get("mode") or "")
        selected_type = str(selection.get("type") or "")
        requested_count = int(selection.get("count") or 0)
        if requested_count > 300:
            return {
                "status": "clarification",
                "state": "scope_too_large",
                "missing": ["smaller_scope"],
                "clarification_type": "scope_too_large",
                "options": [],
                "request": request,
                "scoring": None,
                "filtered_data": {},
                "conversation_state": state_memory,
            }
        count = max(requested_count, 1)
        if mode == "next":
            base = list(state_memory.scope_entities)
            if not base:
                return {
                    "status": "clarification",
                    "state": "missing_scope_start",
                    "missing": ["starting_scope"],
                    "clarification_type": "missing_scope_start",
                    "options": [],
                    "request": request,
                    "scoring": None,
                    "filtered_data": {},
                    "conversation_state": state_memory,
                }
            start = state_memory.scope_end
        else:
            # "first N contracts/customers" is an absolute batch request. Do
            # not reuse the previous focus scope, otherwise a prior batch is
            # carried into the new selection and N becomes N + old scope.
            if not request.get("starting_points") or not request.get("mentioned_entities"):
                if selected_type == "contract":
                    base = [
                        {"type": "contract", "id": str(contract_id)}
                        for contract_id in case_data.get("contracts", {})
                    ]
                elif selected_type == "customer":
                    base = [
                        {"type": "customer", "id": str(customer_id)}
                        for customer_id in case_data.get("customers", {})
                    ]
                else:
                    base = list(request.get("starting_points", []))
            else:
                base = list(request.get("starting_points", []))
            start = 0
        base = [item for item in base if item.get("type") == selected_type]
        selected = base[start:start + count]
        state_memory.scope_entities = base
        request["starting_points"] = selected
        state_memory.scope_start = start + 1
        state_memory.scope_end = start + len(selected)
        if not selected:
            return {
                "status": "clarification",
                "state": "scope_exhausted",
                "missing": [],
                "clarification_type": "scope_exhausted",
                "options": [],
                "request": request,
                "scoring": None,
                "filtered_data": {},
                "conversation_state": state_memory,
            }
    filtered = filter_related_data(case_data, request)
    requested_concerns = list(request.get("requested_concerns", []))
    available_concerns = []
    for item in filtered.get("customer_concerns", []) + filtered.get("contract_concerns", []):
        name = str(item.get("name") or "").upper()
        if name and name not in available_concerns:
            available_concerns.append(name)
    if len(requested_concerns) > 1:
        matching_concerns = [name for name in requested_concerns if name in available_concerns]
        if len(matching_concerns) == 1:
            request["requested_concerns"] = matching_concerns
        else:
            request["requested_concerns"] = matching_concerns or requested_concerns
            state_memory.pending_confirmation = request
            return {
                "status": "clarification",
                "state": "multiple_issues",
                "missing": ["single_issue"],
                "clarification_type": "multiple_issues",
                "options": request["requested_concerns"],
                "request": request,
                "scoring": None,
                "filtered_data": filtered,
                "conversation_state": state_memory,
            }
    state = check_filtered_request(
        filtered,
        request.get("requested_action"),
        request.get("requested_concerns", []),
        sorted(case_data.get("concern_catalog", {})),
    )
    contract_count = len(filtered.get("contracts", []))
    customer_count = len(filtered.get("customers", []))
    too_many_contracts = contract_count > 300
    too_many_customers = not contract_count and customer_count > 300
    if state.get("status") == "ready_for_scoring" and (too_many_contracts or too_many_customers):
        state = {
            "status": "clarification",
            "state": "scope_too_large",
            "missing": ["smaller_scope"],
            "clarification_type": "scope_too_large",
            "options": [],
        }
    scoring = None
    if ledger is not None and state.get("state") == "ready_for_scoring":
        scoring_ledger = deepcopy(ledger) if len(request.get("starting_points", [])) > 1 else ledger
        tentative_scoring = score_entities(
            case_data,
            scoring_ledger,
            request.get("starting_points", []),
            request["requested_concerns"][0],
            team=team,
        )
        finding_statuses = {
            finding.get("status") for finding in tentative_scoring.get("findings", [])
        }
        mixed_group = bool({"new_score", "repeat"} & finding_statuses) and "unsupported" in finding_statuses
        if mixed_group and len(request.get("starting_points", [])) > 1:
            scoring = {
                "status": "unsupported",
                "score_delta": 0,
                "findings": [
                    finding for finding in tentative_scoring.get("findings", [])
                    if finding.get("status") == "unsupported"
                ],
            }
            state = {
                "status": "clarification",
                "state": "mixed_issue",
                "missing": ["specific_entity"],
                "clarification_type": "mixed_issue",
                "options": [],
            }
        else:
            scoring = tentative_scoring
            if scoring_ledger is not ledger:
                ledger.clear()
                ledger.update(scoring_ledger)
    attitude_trace = _update_attitude(state_memory, scoring)
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
        "attitude": state_memory.attitude,
        "attitude_trace": attitude_trace,
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
