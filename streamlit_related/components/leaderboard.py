from __future__ import annotations

from collections import defaultdict
from html import escape
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


def render_leaderboard(
    rows: list[dict[str, str]],
    teams: list[dict[str, str]],
    case_data: dict[str, Any],
    *,
    display_groups: bool = False,
) -> None:
    st.subheader("🏆 Team Scoreboard")
    names = {team["team_id"]: team["team_name"] for team in teams}
    if display_groups:
        team_groups = {team["team_id"]: team.get("group", "DATA") for team in teams}
        rows = [{**row, "team_id": team_groups.get(row["team_id"], "DATA")} for row in rows]
        teams = [{"team_id": group, "team_name": group, "group": group} for group in ("DATA", "PAPER", "AI")]
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
    if not display_groups:
        visible_teams = {f"Team {index}" for index in range(1, 9)}
        summary = [item for item in summary if item["team"] in visible_teams]
    summary.sort(key=lambda item: (-item["score"], item["team"]))
    table_rows = "".join(
        "<tr>"
        f"<td>{index}</td><td class='team'>{escape(str(item['team']))}</td>"
        f"<td>{item['issue_types_found']}</td><td>{item['contracts_found']}</td>"
        f"<td class='score'><span class='score-badge'>{int(item['score'])}</span></td>"
        "</tr>"
        for index, item in enumerate(summary, start=1)
    )
    st.markdown(
        "<style>"
        ".scoreboard-wrap{max-width:900px;margin:0 auto;}"
        ".scoreboard-table{width:100%;border-collapse:separate;border-spacing:0;"
        "border:1px solid rgba(99,102,241,.12);border-radius:14px;overflow:hidden;"
        "font-size:16px;color:#334155;background:#FFFFFF;"
        "box-shadow:0 6px 20px rgba(51,65,85,.045);}"
        ".scoreboard-table th{background:#F8F9FD;color:#7B849B;text-align:left;"
        "font-size:13px;font-weight:600;padding:12px 16px;"
        "letter-spacing:.02em;border-bottom:1px solid rgba(148,163,184,.18);}"
        ".scoreboard-table td{padding:10px 16px;border:0;"
        "text-align:right;background:#FFFFFF;}"
        ".scoreboard-table td.team,.scoreboard-table th.team{text-align:left;}"
        ".scoreboard-table tbody tr+tr td{border-top:1px solid rgba(148,163,184,.12);}"
        ".scoreboard-table td.score{font-weight:700;color:#5963A9;}"
        ".score-badge{display:inline-block;min-width:34px;padding:4px 9px;"
        "border-radius:8px;background:#F0F1FF;text-align:center;}"
        "</style>"
        "<div class='scoreboard-wrap'><table class='scoreboard-table'>"
        "<thead><tr><th>Rank</th><th class='team'>Team</th>"
        "<th>Issues found</th><th>Contracts found</th><th>Score</th></tr></thead>"
        f"<tbody>{table_rows}</tbody></table></div>",
        unsafe_allow_html=True,
    )
