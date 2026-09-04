from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pandas as pd


PUBLIC_COLUMNS = {
    "NarrativeType", "CustomerID", "ContractID", "Issue",
    "PublicNarrative", "SecretNarrative", "AnomalyTags",
}

CUSTOMER_LEVEL_ISSUES = {
    "aml_risk",
    "customer_in_tax_haven",
    "connected_customer_exposure_hidden_by_separate_customer_ids",
}


def normalize_key(value: str) -> str:
    import re
    text = re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")
    return re.sub(r"_+", "_", text)


def concern_level(issue: str) -> str:
    return "customer" if normalize_key(issue) in CUSTOMER_LEVEL_ISSUES else "contract"


def normalize_compact_id(value: str) -> str:
    return "".join(str(value or "").upper().split()).replace("-", "")


def _read_secret_narrative(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _concerns_from_row(row: dict[str, Any]) -> dict[str, dict[str, Any]]:
    concerns: dict[str, dict[str, Any]] = {}
    for item in _read_secret_narrative(row.get("SecretNarrative")):
        issue = str(item.get("Issue type") or "").strip()
        if not issue:
            continue
        explanation = str(
            item.get("Explanation given to auditor") or ""
        ).strip()
        applies_to = re.search(
            r"This applies to \d+ contract\(s\):\s*([^\.]+)\.?",
            explanation,
            flags=re.IGNORECASE,
        )
        concern: dict[str, Any] = {
            "confirmed": True,
            "why_it_violates_policy": str(
                item.get("Why it violates policy") or ""
            ),
            "explanation_for_auditor": explanation,
        }
        if applies_to:
            concern["applies_to_contracts"] = [
                value.strip().upper()
                for value in applies_to.group(1).split(",")
                if value.strip()
            ]
            concern["explanation_for_auditor"] = explanation[applies_to.end():].strip()
        concerns[issue] = concern
    return concerns


def _empty_indexes() -> dict[str, dict[str, list[str]]]:
    names = (
        "customer_to_contracts", "customer_to_assets", "customer_to_vins",
        "contract_to_customers", "contract_to_assets", "contract_to_vins",
        "asset_to_customers", "asset_to_contracts", "asset_to_vins",
        "vin_to_customers", "vin_to_contracts", "vin_to_assets",
    )
    return {name: {} for name in names}


def _add(index: dict[str, list[str]], key: str, value: str) -> None:
    if key and value and value not in index.setdefault(key, []):
        index[key].append(value)


def load_entity_master(path: Path) -> dict[str, Any]:
    empty = {
        "by_contract": {}, "asset_to_contract": {}, "vin_to_contract": {},
        "customer_names": {},
    }
    if not path.exists():
        return empty

    df = pd.read_parquet(path)
    by_contract: dict[str, list[dict[str, str]]] = {}
    asset_to_contract: dict[str, str] = {}
    vin_to_contract: dict[str, str] = {}
    customer_names: dict[str, str] = {}
    for row in df.to_dict("records"):
        lower = {str(key).lower(): value for key, value in row.items()}
        contract = str(lower.get("contract_id") or "").upper().strip()
        customer = str(lower.get("customer_id") or "").upper().strip()
        customer_name = str(lower.get("customer_name") or "").strip()
        asset = str(lower.get("asset_id") or "").upper().strip()
        vin = normalize_compact_id(str(lower.get("vin") or ""))
        if customer and customer_name:
            customer_names[customer] = customer_name
        if contract:
            by_contract.setdefault(contract, []).append({"asset_id": asset, "vin": vin})
        if asset and contract:
            asset_to_contract[asset] = contract
        if vin and contract:
            vin_to_contract[vin] = contract
    return {
        "by_contract": by_contract,
        "asset_to_contract": asset_to_contract,
        "vin_to_contract": vin_to_contract,
        "customer_names": customer_names,
    }


def load_case_data(data_path: Path, entity_path: Path) -> dict[str, Any]:
    df = pd.read_parquet(data_path)
    entity = load_entity_master(entity_path)
    issue_columns = [
        str(column).strip()
        for column in df.columns
        if column not in PUBLIC_COLUMNS and str(column).strip()
    ]
    issue_by_key = {normalize_key(column): column for column in issue_columns}
    contracts: dict[str, dict[str, Any]] = {}
    customers: dict[str, dict[str, Any]] = {}

    for row in df.to_dict("records"):
        customer_id = str(row.get("CustomerID") or "")
        contract_id = str(row.get("ContractID") or "").strip()
        record_id = contract_id or customer_id
        if not record_id:
            continue
        assets = entity["by_contract"].get(contract_id.upper(), []) if contract_id else []
        record = {
            "record_id": record_id,
            "record_type": str(row.get("NarrativeType") or "").lower(),
            "contract_id": contract_id,
            "customer_id": customer_id,
            "customer_name": entity["customer_names"].get(customer_id.upper(), ""),
            "asset_ids": [item["asset_id"] for item in assets if item.get("asset_id")],
            "vins": [item["vin"] for item in assets if item.get("vin")],
            "public_narrative": str(row.get("PublicNarrative") or ""),
            "concerns": _concerns_from_row(row),
        }
        for issue in issue_columns:
            record[normalize_key(issue)] = bool(row.get(issue))
        if contract_id:
            contracts[contract_id.upper()] = record
        elif customer_id:
            customers[customer_id.upper()] = record

    for record in contracts.values():
        customer_id = record["customer_id"].upper()
        if customer_id and customer_id in customers:
            record["customer_name"] = customers[customer_id].get("customer_name", "")

    graph = {
        "customers": {},
        "contracts": {},
        "assets": {},
        "vins": {},
        "concern_catalog": {},
    }
    indexes = _empty_indexes()

    for customer_id, source in customers.items():
        graph["customers"][customer_id] = {
            "customer_id": customer_id,
            "customer_name": source["customer_name"],
            "public_description": source["public_narrative"],
            "customer_concerns": source["concerns"],
            "issue_values": {
                normalize_key(issue): bool(source.get(normalize_key(issue)))
                for issue in issue_columns
            },
            "contract_ids": [],
        }
        for issue, concern in source["concerns"].items():
            graph["concern_catalog"].setdefault(issue, {
                "why_it_violates_policy": concern["why_it_violates_policy"],
                "level": concern_level(issue),
            })

    for contract_id, source in contracts.items():
        customer_id = source["customer_id"].upper()
        graph["contracts"][contract_id] = {
            "contract_id": contract_id,
            "customer_id": customer_id,
            "customer_name": source["customer_name"],
            "public_description": source["public_narrative"],
            "contract_concerns": source["concerns"],
            "issue_values": {
                normalize_key(issue): bool(source.get(normalize_key(issue)))
                for issue in issue_columns
            },
            "asset_ids": source["asset_ids"],
            "vin_ids": source["vins"],
        }
        for issue, concern in source["concerns"].items():
            graph["concern_catalog"].setdefault(issue, {
                "why_it_violates_policy": concern["why_it_violates_policy"],
                "level": concern_level(issue),
            })

        if customer_id in graph["customers"]:
            graph["customers"][customer_id]["contract_ids"].append(contract_id)
        _add(indexes["contract_to_customers"], contract_id, customer_id)
        _add(indexes["customer_to_contracts"], customer_id, contract_id)
        for asset in source["asset_ids"]:
            _add(indexes["contract_to_assets"], contract_id, asset)
            _add(indexes["asset_to_contracts"], asset, contract_id)
            _add(indexes["customer_to_assets"], customer_id, asset)
            _add(indexes["asset_to_customers"], asset, customer_id)
        for vin in source["vins"]:
            _add(indexes["contract_to_vins"], contract_id, vin)
            _add(indexes["vin_to_contracts"], vin, contract_id)
            _add(indexes["customer_to_vins"], customer_id, vin)
            _add(indexes["vin_to_customers"], vin, customer_id)

    for asset_id, contract_ids in indexes["asset_to_contracts"].items():
        graph["assets"][asset_id] = {
            "asset_id": asset_id,
            "contract_ids": contract_ids,
            "customer_ids": indexes["asset_to_customers"].get(asset_id, []),
            "vin_ids": sorted({
                vin for contract_id in contract_ids
                for vin in indexes["contract_to_vins"].get(contract_id, [])
            }),
        }
        for vin in graph["assets"][asset_id]["vin_ids"]:
            _add(indexes["asset_to_vins"], asset_id, vin)
            _add(indexes["vin_to_assets"], vin, asset_id)
            graph["vins"].setdefault(vin, {
                "vin": vin,
                "asset_ids": [],
                "contract_ids": indexes["vin_to_contracts"].get(vin, []),
                "customer_ids": indexes["vin_to_customers"].get(vin, []),
            })
            _add(graph["vins"][vin], "asset_ids", asset_id)

    return {
        "graph": graph,
        "indexes": indexes,
        "contracts": contracts,
        "customers": customers,
        "customer_names": entity["customer_names"],
        "asset_to_contract": entity["asset_to_contract"],
        "vin_to_contract": entity["vin_to_contract"],
        "issue_columns": issue_columns,
        "issue_by_key": issue_by_key,
        "issue_keys": sorted(issue_by_key),
        "data_path": str(data_path),
        "entity_path": str(entity_path),
    }


def contracts_for_customer(case_data: dict[str, Any], customer_id: str) -> list[dict[str, Any]]:
    wanted = str(customer_id or "").upper()
    return [
        record for record in case_data["contracts"].values()
        if str(record.get("customer_id") or "").upper() == wanted
    ]


def contract_id_from_record(record: dict[str, Any]) -> str:
    return str(record.get("contract_id") or record.get("record_id") or "").upper()


def assets_for_customer(case_data: dict[str, Any], customer_id: str) -> list[str]:
    result: list[str] = []
    for record in contracts_for_customer(case_data, customer_id):
        result.extend(item for item in record.get("asset_ids", []) if item not in result)
    return result


def vins_for_customer(case_data: dict[str, Any], customer_id: str) -> list[str]:
    result: list[str] = []
    for record in contracts_for_customer(case_data, customer_id):
        result.extend(
            item for item in record.get("vin_ids", record.get("vins", []))
            if item not in result
        )
    return result
