from __future__ import annotations

import re
from typing import Any

from stage_01_case_data import contract_id_from_record, contracts_for_customer, normalize_compact_id, normalize_key


def _relationship_index(case_data: dict[str, Any], name: str) -> dict[str, list[str]]:
    return case_data.get("indexes", {}).get(name, case_data.get(name, {}))


def _concerns(record: dict[str, Any], entity_type: str) -> dict[str, Any]:
    return record.get(
        "customer_concerns" if entity_type == "customer" else "contract_concerns",
        record.get("concerns", {}),
    )


def _matching_concerns(
    case_data: dict[str, Any], record: dict[str, Any], entity_type: str,
    requested: set[str],
) -> list[tuple[str, dict[str, Any]]]:
    existing = _concerns(record, entity_type)
    catalog = case_data.get("concern_catalog", {})
    result = []
    for name, concern in existing.items():
        if name.upper() in requested:
            result.append((name, concern))
    values = record.get("issue_values", {})
    for name in requested:
        key = normalize_key(name)
        if values.get(key) and not any(item[0].upper() == name for item in result):
            definition = catalog.get(name, {})
            result.append((name, {
                "confirmed": True,
                "why_it_violates_policy": definition.get("why_it_violates_policy", ""),
                "explanation_for_auditor": "",
            }))
    return result


def _record_vins(record: dict[str, Any]) -> list[str]:
    return list(record.get("vin_ids", record.get("vins", [])))


def resolve_target(case_data: dict[str, Any], kind: str, value: str) -> dict[str, Any]:
    kind = str(kind or "").lower()
    value = str(value or "").strip().upper()
    if kind == "asset":
        contract_ids = _relationship_index(case_data, "asset_to_contracts").get(value, [])
        contract_id = contract_ids[0] if contract_ids else case_data.get("asset_to_contract", {}).get(value)
        if not contract_id:
            return {"status": "not_found", "input_kind": kind, "input_id": value}
        record = case_data["contracts"].get(contract_id)
        return _target(case_data, kind, value, contract_id, record)
    if kind == "vin":
        contract_ids = _relationship_index(case_data, "vin_to_contracts").get(value, [])
        contract_id = contract_ids[0] if contract_ids else case_data.get("vin_to_contract", {}).get(value)
        if not contract_id:
            return {"status": "not_found", "input_kind": kind, "input_id": value}
        record = case_data["contracts"].get(contract_id)
        return _target(case_data, kind, value, contract_id, record)
    if kind == "contract" and value in case_data.get("contracts", {}):
        return _target(case_data, kind, value, value, case_data["contracts"][value])
    if kind == "customer" and value in case_data.get("customers", {}):
        records = contracts_for_customer(case_data, value)
        return {
            "status": "resolved", "input_kind": kind, "input_id": value,
            "customer_id": value, "contract_ids": [contract_id_from_record(r) for r in records],
            "asset_ids": _unique(item for r in records for item in r.get("asset_ids", [])),
            "vins": _unique(item for r in records for item in _record_vins(r)),
            "record": case_data["customers"][value],
        }
    return {"status": "not_found", "input_kind": kind, "input_id": value}


def _target(case_data: dict[str, Any], kind: str, value: str, contract_id: str, record: dict[str, Any] | None) -> dict[str, Any]:
    if not record:
        return {"status": "not_found", "input_kind": kind, "input_id": value}
    return {
        "status": "resolved", "input_kind": kind, "input_id": value,
        "contract_id": contract_id,
        "customer_id": str(record.get("customer_id") or "").upper(),
        "asset_ids": list(record.get("asset_ids", [])),
        "vins": _record_vins(record),
        "record": record,
    }


def resolve_text(case_data: dict[str, Any], text: str) -> dict[str, Any]:
    source = str(text or "")
    candidates: list[tuple[str, str]] = []
    patterns = (
        ("contract", r"\bS\s*E[\s_-]*\d{6}\b"),
        ("customer", r"\bC\s*U\s*S\s*T[\s_-]*\d{1,4}\b"),
        ("asset", r"\bA\s*S\s*T[\s_-]*\d{6}\b"),
        ("vin", r"\b[A-HJ-NPR-Z0-9]{17}\b"),
    )
    for kind, pattern in patterns:
        candidates.extend((kind, match.group(0)) for match in re.finditer(pattern, source, re.IGNORECASE))
    normalized_candidates = []
    for kind, value in candidates:
        compact = normalize_compact_id(value)
        if kind == "contract":
            compact = "SE" + compact[2:]
        elif kind == "customer":
            compact = "CUST" + compact[4:].zfill(4)
        elif kind == "asset":
            compact = "AST" + compact[3:]
        normalized_candidates.append((kind, compact))
    targets = [resolve_target(case_data, kind, value) for kind, value in normalized_candidates]
    resolved = [target for target in targets if target["status"] == "resolved"]
    refs = _unique(
        target.get("contract_id") or target.get("customer_id")
        for target in resolved
    )
    return {
        "targets": resolved,
        "refs": refs,
        "contracts": _unique(target.get("contract_id") for target in resolved if target.get("contract_id")),
        "customers": _unique(target.get("customer_id") for target in resolved if target.get("customer_id")),
        "unmatched": [target for target in targets if target["status"] != "resolved"],
        "ambiguous": [],
    }


def filter_related_data(
    case_data: dict[str, Any], request: dict[str, Any]
) -> dict[str, Any]:
    """Filter graph data from structured LLM1 entity mentions.

    This only resolves relationships and concerns. It does not score.
    """
    selected = {"customers": [], "contracts": [], "assets": [], "vins": []}
    starts = request.get("start_from", request.get("starting_points", []))
    for start in starts:
        target = resolve_target(case_data, start.get("type", ""), start.get("id", ""))
        if target.get("status") != "resolved":
            continue
        _append(selected["customers"], target.get("customer_id"))
        _append(selected["contracts"], target.get("contract_id"))
        for asset_id in target.get("asset_ids", []):
            _append(selected["assets"], asset_id)
        for vin in target.get("vins", []):
            _append(selected["vins"], vin)

    requested_concerns = {
        str(name).strip().upper() for name in request.get(
            "concerns", request.get("requested_concerns", [])
        )
    }
    customer_concerns = []
    contract_concerns = []
    if requested_concerns:
        for customer_id in selected["customers"]:
            record = case_data.get("customers", {}).get(customer_id, {})
            for name, concern in _matching_concerns(case_data, record, "customer", requested_concerns):
                customer_concerns.append({"customer_id": customer_id, "name": name, **concern})
        for contract_id in selected["contracts"]:
            record = case_data.get("contracts", {}).get(contract_id, {})
            for name, concern in _matching_concerns(case_data, record, "contract", requested_concerns):
                contract_concerns.append({"contract_id": contract_id, "name": name, **concern})

    return {
        "starting_points": starts,
        "customers": selected["customers"],
        "contracts": selected["contracts"],
        "assets": selected["assets"],
        "vins": selected["vins"],
        "customer_concerns": customer_concerns,
        "contract_concerns": contract_concerns,
    }


def expand_conversation_references(
    case_data: dict[str, Any],
    references: list[dict[str, Any]],
    latest_messages: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Turn LLM1 conversation references into graph-validated start points."""
    expanded: list[dict[str, str]] = []
    for reference in references:
        candidates = list(reference.get("resolved_entities") or [])
        if not candidates:
            message_number = reference.get("source_message")
            source = next(
                (item for item in latest_messages
                 if item.get("message_number") == message_number),
                {},
            )
            text = " ".join(filter(None, [
                str(source.get("content") or ""),
                str(source.get("image_text") or ""),
            ]))
            selection = reference.get("selection") or {}
            wanted_type = str(selection.get("type") or "")
            candidates = [
                {"type": wanted_type, "id": target.get("input_id")}
                for target in resolve_text(case_data, text)["targets"]
                if not wanted_type or target.get("input_kind") == wanted_type
            ]
        for candidate in candidates:
            kind = str(candidate.get("type") or "")
            value = str(candidate.get("id") or "")
            target = resolve_target(case_data, kind, value)
            if target.get("status") != "resolved":
                continue
            if kind == "customer":
                expanded.append({"type": kind, "id": target["customer_id"]})
            elif kind == "contract":
                expanded.append({"type": kind, "id": target["contract_id"]})
            elif kind == "asset":
                expanded.append({"type": kind, "id": target["input_id"]})
            elif kind == "vin":
                expanded.append({"type": kind, "id": target["input_id"]})
    return _unique_dicts(expanded)


def _unique(values):
    result = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _append(values: list[str], value: str | None) -> None:
    if value and value not in values:
        values.append(value)


def _unique_dicts(values: list[dict[str, str]]) -> list[dict[str, str]]:
    result = []
    for value in values:
        if value not in result:
            result.append(value)
    return result
