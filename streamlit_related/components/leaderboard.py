from __future__ import annotations

from collections import defaultdict
from typing import Any

import pandas as pd
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
            {"team_id": team_id, "issue_types": set(), "contracts": set()},
        )
        entry["issue_types"].add(row["issue_type"])
        if row["record_type"] == "customer":
            entry["contracts"].update(contracts_by_customer.get(row["record_id"], set()))
        else:
            entry["contracts"].add(row["record_id"])
    return [
        {
            "team_id": entry["team_id"],
            "issue_types_found": len(entry["issue_types"]),
            "contracts_found": len(entry["contracts"]),
            "score": len(entry["issue_types"]) * len(entry["contracts"]),
        }
        for entry in grouped.values()
    ]


def render_leaderboard(rows: list[dict[str, str]], teams: list[dict[str, str]], case_data: dict[str, Any]) -> None:
    st.subheader("🏆 Team Scoreboard")
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
    scoreboard_df = pd.DataFrame([
            {
                "Rank": index,
                "Team": item["team"],
                "Issues found": item["issue_types_found"],
                "Contracts found": item["contracts_found"],
                "Score": int(item["score"]),
            }
            for index, item in enumerate(summary, start=1)
        ])
    st.dataframe(
        scoreboard_df,
        hide_index=True,
        column_config={
            "Rank": st.column_config.NumberColumn(width="small"),
            "Team": st.column_config.TextColumn(width="medium"),
            "Issues found": st.column_config.NumberColumn(width="small"),
            "Contracts found": st.column_config.NumberColumn(width="small"),
            "Score": st.column_config.NumberColumn(width="small"),
        },
        width=760,
        height=min(520, 38 * (len(scoreboard_df) + 1) + 8),
    )
