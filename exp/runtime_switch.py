"""Single runtime switch; manual runtime remains the default."""

from __future__ import annotations

import os
from typing import Any

from shared.audit_rpg import run_agent_turn as run_manual_turn


def run_agent_turn(
    chat_history: list[dict[str, Any]],
    ledger: dict[str, dict[str, Any]],
    case_data: dict[str, Any],
    model: str | None = None,
    active_refs: list[str] | None = None,
    active_investigation_scope: dict[str, list[str]] | None = None,
) -> tuple[str, list[dict[str, Any]], dict[str, list[str]]]:
    runtime_name = os.getenv("AUDIT_RPG_RUNTIME", "experiment").strip().lower()
    if runtime_name == "experiment":
        from exp.simplified_exp.run_experiment import run_investigation

        latest_user = next((item for item in reversed(chat_history) if item.get("role") == "user"), {})
        if active_investigation_scope is None:
            active_investigation_scope = {
                "contracts": [],
                "customers": [],
                "assets": [],
                "vins": [],
            }
            for ref in active_refs or []:
                ref_text = str(ref)
                if ref_text in case_data.get("contracts", {}):
                    active_investigation_scope["contracts"].append(ref_text)
                elif ref_text in case_data.get("customers", {}):
                    active_investigation_scope["customers"].append(ref_text)
                elif ref_text in case_data.get("asset_to_contract", {}):
                    active_investigation_scope["assets"].append(ref_text)
                elif ref_text in case_data.get("vin_to_contract", {}):
                    active_investigation_scope["vins"].append(ref_text)
        result = run_investigation(
            str(latest_user.get("content") or ""),
            case_data,
            ledger,
            model or os.getenv("AUDIT_RPG_MODEL", "gpt-4.1"),
            image_data_urls=list(latest_user.get("images") or []),
            chat_history=chat_history,
            active_investigation_scope=active_investigation_scope,
        )
        score_result = result.get("score_result") or {}
        events = []
        if result.get("status") == "lookup":
            events.append({
                "tool": "find_records",
                "output": {
                    "status": "lookup",
                    "records": result.get("records", []),
                },
            })
        if score_result.get("findings"):
            events.append({"tool": "update_score", "output": score_result})
        return result["reply"], events, result.get("active_investigation_scope", active_investigation_scope)

    if runtime_name == "structured":
        from test.legacy.structured_runtime import run_structured_turn

        reply, events = run_structured_turn(chat_history, ledger, case_data, model, active_refs)
        return reply, events, active_investigation_scope or {"contracts": [], "customers": [], "assets": [], "vins": []}
    reply, events = run_manual_turn(chat_history, ledger, case_data, model, active_refs)
    return reply, events, active_investigation_scope or {"contracts": [], "customers": [], "assets": [], "vins": []}