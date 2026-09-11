from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from typing import Any, Callable

from audit_types import AuditRequest
from audit_types import DecisionResult
from stage_03_request_parser import (
    merge_pending_request,
    parse_conversation_request,
    resolved_request_from_dict,
)
from stage_01_case_data import normalize_key
from stage_04_entity_resolution import (
    expand_conversation_references,
    filter_related_data,
    resolve_target,
    selected_data_from_filtered,
)
from stage_05_verification import verify_request
from stage_05_verification import check_filtered_request
from stage_06_scoring import score_entities
from conversation_state import ConversationState


def decision_result_from_scoring(scoring: dict[str, Any] | None) -> DecisionResult:
    """Create the Phase C value object from the legacy scoring result."""
    scoring = scoring or {}
    findings = list(scoring.get("findings") or [])
    return DecisionResult(
        status=str(scoring.get("status") or "not_run"),
        findings=findings,
        score_delta=int(scoring.get("score_delta") or 0),
        confirmed_count=sum(
            item.get("status") in {"new_score", "repeat"} for item in findings
        ),
        unsupported_count=sum(
            item.get("status") == "unsupported" for item in findings
        ),
        repeat_count=sum(item.get("status") == "repeat" for item in findings),
    )


def _attitude_stage(pressure: int) -> str:
    if pressure <= 0:
        return "confident"
    if pressure <= 50:
        return "embarrassed"
    if pressure <= 150:
        return "guarded"
    if pressure <= 400:
        return "defensive"
    if pressure <= 900:
        return "nervous"
    return "defeated"


def _update_attitude(
    state: ConversationState,
    scoring: dict[str, Any] | None,
) -> dict[str, Any]:
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
    if new_count:
        trigger = "new_score"
        pressure_delta = new_count
    elif unsupported_count:
        trigger = "unsupported"
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
    candidates = [
        item for item in parsed.get("issue_candidates", [])
        if isinstance(item, dict)
        and str(item.get("issue") or "").strip()
        and str(item.get("description") or "").strip()
    ]
    if len(candidates) >= 2:
        confidence_total = sum(
            max(0.0, float(item.get("confidence") or 0))
            for item in candidates
        )
        if confidence_total:
            candidates = [
                {
                    **item,
                    "confidence": round(
                        max(0.0, float(item.get("confidence") or 0))
                        / confidence_total,
                        3,
                    ),
                }
                for item in candidates
            ]
            parsed["issue_candidates"] = candidates
    if len(candidates) == 1:
        parsed["requested_concerns"] = [str(candidates[0]["issue"]).strip().upper()]
        parsed["requested_action"] = parsed.get("requested_action") or "assess"
        parsed["needs_issue_clarification"] = False
    elif len(candidates) >= 2:
        ranked_candidates = sorted(
            candidates,
            key=lambda item: float(item.get("confidence") or 0),
            reverse=True,
        )
        top_confidence = float(ranked_candidates[0].get("confidence") or 0)
        if top_confidence > 0.5:
            parsed["requested_concerns"] = [
                str(ranked_candidates[0]["issue"]).strip().upper()
            ]
            parsed["requested_action"] = parsed.get("requested_action") or "assess"
            parsed["needs_issue_clarification"] = False
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
    if (
        pending_issues
        and not parsed.get("requested_concerns")
        and parsed.get("requested_action") == "explain"
    ):
        parsed["requested_concerns"] = pending_issues
    continuation_text = message.casefold()
    is_issue_continuation = any(
        phrase in continuation_text
        for phrase in ("same issue", "same concern", "more for this", "more on this")
    )
    if is_issue_continuation and pending_issues:
        parsed["requested_concerns"] = pending_issues
    request = merge_pending_request(state_memory.pending_confirmation, parsed)
    resolved_request = None
    message_key = normalize_key(message)
    name_matches = []
    for customer_id, record in case_data.get("customers", {}).items():
        customer_name = normalize_key(record.get("customer_name", ""))
        short_name = customer_name.removesuffix("_ab")
        if short_name and short_name in message_key:
            name_matches.append((len(short_name), customer_id))
    has_new_scope = bool(parsed.get("mentioned_entities") or name_matches)
    unresolved_entity_mentions = list(parsed.get("unresolved_entity_mentions") or [])
    if unresolved_entity_mentions and not has_new_scope:
        return {
            "status": "not_found",
            "state": "missing_entity",
            "missing": ["entity"],
            "clarification_type": "missing_entity",
            "options": [],
            "request": request,
            "unresolved_entity_mentions": unresolved_entity_mentions,
            "scoring": None,
            "filtered_data": {},
            "conversation_state": state_memory,
        }
    if (
        request.get("requested_concerns")
        and request.get("requested_action") in {"overview", "lookup"}
        and has_new_scope
    ):
        request["requested_action"] = "assess"
    if request.get("response_mode") == "broader_scope_follow_up" and not has_new_scope:
        request["requested_action"] = "overview"
    current_points = [
        {"type": item.get("type"), "id": item.get("id")}
        for item in parsed.get("mentioned_entities", [])
    ]
    current_points.extend(
        {"type": "customer", "id": customer_id}
        for _, customer_id in sorted(name_matches, reverse=True)
    )
    explicit_contract_points = [
        point for point in current_points if point.get("type") == "contract"
    ]
    if explicit_contract_points:
        # An explicit contract is the narrowest subject. Do not expand a
        # simultaneously mentioned customer into all of its contracts.
        current_points = explicit_contract_points
    if current_points:
        # A newly explicit entity starts a new scope; never merge it with the
        # previous conversational focus.
        request["starting_points"] = current_points
    else:
        request["starting_points"] = list(request.get("starting_points") or [])
    if not current_points:
        request["starting_points"].extend(expand_conversation_references(
            case_data, parsed.get("references", []), latest_messages
        ))
    if not request["starting_points"]:
        request["starting_points"] = list(state_memory.focus_entities)
    canonical_points = []
    seen_canonical_points: set[tuple[str, str]] = set()
    for point in request["starting_points"]:
        target = resolve_target(case_data, point.get("type", ""), point.get("id", ""))
        if target.get("status") != "resolved":
            continue
        resolved_points = []
        if point.get("type") == "customer":
            resolved_points = [{"type": "customer", "id": target["customer_id"]}]
        elif point.get("type") == "contract":
            resolved_points = [{"type": "contract", "id": target["contract_id"]}]
        elif point.get("type") in {"asset", "vin"}:
            resolved_points = [
                {"type": "contract", "id": contract_id}
                for contract_id in target.get("contract_ids", [])
            ]
        else:
            resolved_points = [point]
        for resolved_point in resolved_points:
            key = (str(resolved_point.get("type")), str(resolved_point.get("id")))
            if key not in seen_canonical_points:
                canonical_points.append(resolved_point)
                seen_canonical_points.add(key)
    if canonical_points:
        request["starting_points"] = canonical_points
    resolved_request = resolved_request_from_dict(request)
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
    selected_data = selected_data_from_filtered(filtered)
    requested_concerns = list(request.get("requested_concerns", []))
    available_concerns = []
    for item in filtered.get("customer_concerns", []) + filtered.get("contract_concerns", []):
        name = str(item.get("name") or "").upper()
        if name and name not in available_concerns:
            available_concerns.append(name)
    if len(requested_concerns) > 1:
        matching_concerns = [name for name in requested_concerns if name in available_concerns]
        request["requested_concerns"] = matching_concerns or requested_concerns
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
        requested_issues = request["requested_concerns"]
        single_subject_multi_issue = (
            len(request.get("starting_points", [])) == 1
            and len(requested_issues) > 1
        )
        if single_subject_multi_issue:
            issue_results = [
                score_entities(
                    case_data,
                    scoring_ledger,
                    request.get("starting_points", []),
                    issue,
                    team=team,
                )
                for issue in requested_issues
            ]
            findings = [
                finding
                for result in issue_results
                for finding in result.get("findings", [])
            ]
            tentative_scoring = {
                "status": "new_score" if any(
                    item.get("status") == "new_score" for item in findings
                ) else "repeat" if any(
                    item.get("status") == "repeat" for item in findings
                ) else "unsupported",
                "score_delta": sum(
                    int(item.get("score_delta") or (
                        1 if item.get("status") == "new_score" else 0
                    ))
                    for item in findings
                ),
                "findings": findings,
            }
        else:
            tentative_scoring = score_entities(
                case_data,
                scoring_ledger,
                request.get("starting_points", []),
                requested_issues[0],
                team=team,
            )
        finding_statuses = {
            finding.get("status") for finding in tentative_scoring.get("findings", [])
        }
        mixed_group = bool({"new_score", "repeat"} & finding_statuses) and "unsupported" in finding_statuses
        if mixed_group:
            findings = tentative_scoring.get("findings", [])
            confirmed_count = sum(
                item.get("status") in {"new_score", "repeat"}
                for item in findings
            )
            unsupported_count = sum(
                item.get("status") == "unsupported" for item in findings
            )
            if confirmed_count >= unsupported_count:
                scoring = dict(tentative_scoring)
                scoring["status"] = "partial_confirmed"
                scoring["score_delta"] = sum(
                    item.get("status") == "new_score" for item in findings
                )
                if scoring_ledger is not ledger:
                    ledger.clear()
                    ledger.update(scoring_ledger)
            else:
                scoring = {
                    "status": "mixed_issue",
                    "score_delta": 0,
                    "findings": findings,
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
    previous_focus_entities = list(state_memory.focus_entities)
    if request.get("starting_points"):
        state_memory.focus_entities = request["starting_points"]
    if request.get("requested_concerns") or request.get("requested_action"):
        current_issue = (request.get("requested_concerns") or [None])[0]
        existing_issue = (state_memory.focus_topic or {}).get("issue")
        same_focus = (
            not request.get("starting_points")
            or not previous_focus_entities
            or any(
                point in previous_focus_entities
                for point in request.get("starting_points", [])
            )
        )
        if (
            current_issue is None
            and existing_issue
            and same_focus
            and request.get("requested_action") in {"explain", "overview", "lookup"}
        ):
            current_issue = existing_issue
        state_memory.focus_topic = {
            "action": request.get("requested_action"),
            "issue": current_issue,
        }
    state_memory.pending_confirmation = request if state["status"] == "clarification" else None
    decision_result = decision_result_from_scoring(scoring)
    return {
        **state,
        "request": request,
        "resolved_request": resolved_request,
        "scoring": scoring,
        "decision_result": asdict(decision_result),
        "attitude": state_memory.attitude,
        "attitude_trace": attitude_trace,
        "filtered_data": filtered,
        "selected_data": asdict(selected_data),
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
