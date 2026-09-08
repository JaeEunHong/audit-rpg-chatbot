from __future__ import annotations

from collections import defaultdict
from typing import Any

import streamlit as st


def leaderboard_summary(rows: list[dict[str, str]], case_data: dict[str, Any]) -> list[dict[str, Any]]:
    contracts_by_customer: dict[str, set[str]] = defaultdict(set)
    for contract_id, record in case_data.get("contracts", {}).items():
        customer_id = str(record.get("customer_id") or "")
        if customer_id:
            contracts_by_customer[customer_id].add(str(contract_id))

    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        team_id = row["team_id"]
        entry = grouped.setdefault(
            team_id,
            {"team_id": team_id, "issue_types": set(), "contracts": set(), "score": 0.0},
        )
        entry["issue_types"].add(row["issue_type"])
        entry["score"] += float(row.get("score_delta", 0) or 0)
        if row["record_type"] == "customer":
            entry["contracts"].update(contracts_by_customer.get(row["record_id"], set()))
        else:
            entry["contracts"].add(row["record_id"])
    return [
        {
            "team_id": entry["team_id"],
            "issue_types_found": len(entry["issue_types"]),
            "contracts_found": len(entry["contracts"]),
            "score": entry["score"],
        }
        for entry in grouped.values()
    ]


def render_leaderboard(rows: list[dict[str, str]], teams: list[dict[str, str]], case_data: dict[str, Any]) -> None:
    names = {team["team_id"]: team["team_name"] for team in teams}
    summary = leaderboard_summary(rows, case_data)
    summary_by_team = {item["team_id"]: item for item in summary}
    # Keep the scoreboard stable: teams with no scored findings should still
    # be visible, especially in the demo workspace.
    for team in teams:
        summary_by_team.setdefault(
            team["team_id"],
            {
                "team_id": team["team_id"],
                "issue_types_found": 0,
                "contracts_found": 0,
                "score": 0,
            },
        )
    summary = list(summary_by_team.values())
    for item in summary:
        item["team"] = names.get(item["team_id"], item["team_id"])
    summary.sort(key=lambda item: (-item["score"], item["team"]))
    st.dataframe(
        [
            {
                "Team": item["team"],
                "Issue types": item["issue_types_found"],
                "Contracts": item["contracts_found"],
                "Score": item["score"],
            }
            for item in summary
        ],
        hide_index=True,
        use_container_width=True,
    )
