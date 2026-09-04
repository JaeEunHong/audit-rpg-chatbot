from __future__ import annotations

from typing import Any

from stage_01_case_data import contract_id_from_record, contracts_for_customer
from stage_04_entity_resolution import resolve_target
from stage_06_scoring import resolve_issue_key, score_claim


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
    if requested_action == "assess":
        return {"status": "ready_for_scoring", "state": "ready_for_scoring", "missing": [], "clarification_type": None, "options": []}
    return {"status": "ready_for_lookup", "state": "lookup", "missing": [], "clarification_type": None, "options": []}


def verify_request(request, case_data: dict[str, Any], ledger: dict[str, Any]) -> dict[str, Any]:
    targets = []
    missing = []
    for mention in request.entity_mentions:
        target = resolve_target(case_data, mention.get("kind", ""), mention.get("text", ""))
        if target["status"] == "resolved":
            targets.append((mention, target))
        else:
            missing.append(mention.get("text", ""))

    if missing and not targets:
        return {"status": "not_found", "records": [], "clarification_kind": "missing_entity"}
    if not targets:
        return {"status": "clarification", "records": [], "clarification_kind": "missing_entity"}

    if not request.issue_claims:
        if request.requested_content in {
            "overview", "identity", "asset_details", "vin", "date",
            "interest_rate", "exposure", "approval",
        }:
            return {"status": "lookup", "records": [target["record"] for _, target in targets]}
        return {
            "status": "clarification", "records": [],
            "clarification_kind": "missing_issue",
        }

    findings = []
    for claim in request.issue_claims:
        mention_id = claim.get("mention_id")
        matches = [(mention, target) for mention, target in targets if mention.get("mention_id") == mention_id]
        if not matches:
            continue
        _, target = matches[0]
        issue_type = str(claim.get("candidate_issue") or "")
        issue_key = resolve_issue_key(case_data, issue_type)
        if not issue_key:
            continue
        contract_id = target.get("contract_id")
        if not contract_id and target.get("input_kind") == "customer":
            contracts = contracts_for_customer(case_data, target["customer_id"])
            if len(contracts) != 1:
                return {
                    "status": "needs_contract_examples", "records": [target["record"]],
                    "clarification_kind": "missing_entity",
                    "reason": "The concern needs a specific contract example.",
                }
            contract_id = contract_id_from_record(contracts[0])
        if contract_id:
            result = score_claim(case_data, ledger, contract_id, issue_type, claim.get("rationale", ""))
            findings.extend(result.get("findings", []))

    if not findings:
        return {"status": "clarification", "records": [target["record"] for _, target in targets], "clarification_kind": "missing_issue"}
    delta = sum(item.get("score_delta", 0) for item in findings)
    return {
        "status": "new_score" if delta else "repeat" if any(item["status"] == "repeat" for item in findings) else "unsupported",
        "records": [target["record"] for _, target in targets],
        "score_result": {"status": "new_score" if delta else "repeat" if any(item["status"] == "repeat" for item in findings) else "unsupported", "score_delta": delta, "findings": findings},
    }
