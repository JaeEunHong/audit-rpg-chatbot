from __future__ import annotations

from typing import Any

from stage_01_case_data import normalize_key


def issue_is_true(record: dict[str, Any], issue_key: str) -> bool:
    return bool(record.get("issue_values", {}).get(issue_key, record.get(issue_key)))


def total_score(ledger: dict[str, Any]) -> int:
    issue_types = {entry.get("issue_type") for entry in ledger.values() if entry.get("issue_type")}
    contract_ids = {entry.get("contract_id") for entry in ledger.values() if entry.get("contract_id")}
    return len(issue_types) * len(contract_ids)


def resolve_issue_key(case_data: dict[str, Any], issue_type: str) -> str | None:
    key = normalize_key(issue_type)
    if key in case_data.get("issue_by_key", {}):
        return key
    compact = key.replace("_", "")
    for candidate in case_data.get("issue_keys", []):
        if candidate.replace("_", "") == compact:
            return candidate
    return None


def score_claim(case_data: dict[str, Any], ledger: dict[str, Any], contract_id: str, issue_type: str, rationale: str = "", team: str = "default") -> dict[str, Any]:
    issue_key = resolve_issue_key(case_data, issue_type)
    record = case_data.get("contracts", {}).get(str(contract_id).upper())
    if not issue_key:
        return {"status": "unsupported", "reason": "Issue is not in the runtime catalog.", "score_delta": 0, "findings": []}
    if not record:
        return {"status": "not_found", "reason": "Contract was not found.", "score_delta": 0, "findings": []}

    record_id = record.get("record_id", record.get("contract_id", str(contract_id).upper()))
    key = f"{team}::{record_id}::{issue_key}"
    truth = issue_is_true(record, issue_key)
    if not truth:
        status = "unsupported"
        reason = "The issue is not true for this record."
    elif key in ledger:
        status = "repeat"
        reason = "Finding already scored."
    else:
        status = "new_score"
        reason = "Finding matches the record data."
        ledger[key] = {
            "record_id": record_id,
            "contract_id": record["contract_id"],
            "customer_id": record["customer_id"],
            "issue_type": case_data["issue_by_key"][issue_key],
            "issue_key": issue_key,
            "status": status,
            "rationale": rationale,
        }
    finding = {
        "record_id": record_id,
        "contract_id": record["contract_id"],
        "customer_id": record["customer_id"],
        "asset_ids": list(record.get("asset_ids", [])),
        "vins": list(record.get("vins", [])),
        "issue_type": case_data["issue_by_key"][issue_key],
        "issue_key": issue_key,
        "status": status,
        "score_delta": int(status == "new_score"),
        "reason": reason,
        "rationale": rationale,
    }
    return {
        "status": status,
        "issue_type": finding["issue_type"],
        "score_delta": finding["score_delta"],
        "findings": [finding],
    }


def score_entities(
    case_data: dict[str, Any],
    ledger: dict[str, Any],
    entities: list[dict[str, str]],
    issue_type: str,
    *,
    team: str = "default",
) -> dict[str, Any]:
    """Score one issue across the requested entities without partial scoring."""
    issue_key = resolve_issue_key(case_data, issue_type)
    if not issue_key:
        return {"status": "unsupported", "score_delta": 0, "findings": []}

    scope = case_data.get("concern_catalog", {}).get(issue_type, {}).get("level", "contract")
    contract_ids: list[str] = []
    if scope == "customer":
        customer_ids = []
        for entity in entities:
            if entity.get("type") == "customer":
                customer_ids.append(entity.get("id", ""))
            elif entity.get("type") == "contract":
                record = case_data.get("contracts", {}).get(entity.get("id", ""))
                if record:
                    customer_ids.append(record.get("customer_id", ""))
        customer_ids = list(dict.fromkeys(item for item in customer_ids if item))
        customer_records = [case_data.get("customers", {}).get(customer_id) for customer_id in customer_ids]
        if not customer_records:
            return {"status": "unsupported", "score_delta": 0, "findings": []}
        customer_findings = []
        for customer_id, record in zip(customer_ids, customer_records):
            concern = record.get("customer_concerns", {}).get(issue_type)
            if not issue_is_true(record, issue_key) and not (concern and concern.get("confirmed")):
                customer_findings.append({
                    "customer_id": customer_id,
                    "issue_type": case_data["issue_by_key"][issue_key],
                    "status": "unsupported",
                    "reason": "The issue is not true for this customer.",
                })
                continue
            listed_contracts = (concern or {}).get("applies_to_contracts", [])
            ids = listed_contracts or record.get("contract_ids", [])
            for contract_id in ids:
                contract = case_data.get("contracts", {}).get(contract_id)
                if not contract:
                    continue
                record_id = contract.get("record_id", contract_id)
                ledger_key = f"{team}::{record_id}::{issue_key}"
                status = "repeat" if ledger_key in ledger else "new_score"
                if status == "new_score":
                    ledger[ledger_key] = {
                        "record_id": record_id,
                        "contract_id": contract_id,
                        "customer_id": customer_id,
                        "issue_type": case_data["issue_by_key"][issue_key],
                        "issue_key": issue_key,
                        "status": status,
                        "team": team,
                    }
                customer_findings.append({
                    "contract_id": contract_id,
                    "customer_id": customer_id,
                    "issue_type": case_data["issue_by_key"][issue_key],
                    "status": status,
                    "reason": (concern or {}).get("explanation_for_auditor", ""),
                })
        new_count = sum(item["status"] == "new_score" for item in customer_findings)
        repeat_count = sum(item["status"] == "repeat" for item in customer_findings)
        matched_count = sum(item["status"] in {"new_score", "repeat"} for item in customer_findings)
        overall_status = "new_score" if new_count else "repeat" if repeat_count else "unsupported"
        return {
            "status": overall_status,
            "score_delta": new_count,
            "score": new_count,
            "score_summary": {
                "new_contract_count": new_count,
                "matched_contract_count": matched_count,
                "total_score": total_score(ledger),
            },
            "issue_type": case_data["issue_by_key"][issue_key],
            "findings": customer_findings,
        }
    else:
        contract_ids = [entity.get("id", "") for entity in entities if entity.get("type") == "contract"]

    contract_ids = list(dict.fromkeys(item for item in contract_ids if item))
    records = [case_data.get("contracts", {}).get(contract_id) for contract_id in contract_ids]
    if not records:
        return {"status": "unsupported", "score_delta": 0, "findings": []}

    findings = [
        score_claim(case_data, ledger, contract_id, issue_type, team=team)["findings"]
        for contract_id in contract_ids
    ]
    findings = [item for group in findings for item in group]

    new_findings = [item for item in findings if item.get("status") == "new_score"]
    repeat_findings = [item for item in findings if item.get("status") == "repeat"]
    return {
        "status": "repeat" if not new_findings and repeat_findings else "new_score",
        "score_delta": len(new_findings),
        "score": len(new_findings),
        "score_summary": {
            "new_contract_count": len(new_findings),
            "matched_contract_count": len(new_findings) + len(repeat_findings),
            "total_score": total_score(ledger),
        },
        "team": team,
        "issue_type": case_data["issue_by_key"][issue_key],
        "findings": findings,
    }
