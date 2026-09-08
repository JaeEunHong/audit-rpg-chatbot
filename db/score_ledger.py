from __future__ import annotations

from typing import Any

from .connection import snowflake_cursor


def ensure_score_ledger_table() -> None:
    with snowflake_cursor() as cursor:
        cursor.execute(
            """CREATE TABLE IF NOT EXISTS score_ledger (
                team_id VARCHAR NOT NULL,
                record_id VARCHAR NOT NULL,
                record_type VARCHAR NOT NULL,
                issue_type VARCHAR NOT NULL,
                timestamp TIMESTAMP_NTZ NOT NULL
            )"""
        )


def insert_verified_findings(team_id: str, findings: list[dict[str, Any]]) -> None:
    if not team_id or not findings:
        return
    ensure_score_ledger_table()
    with snowflake_cursor() as cursor:
        for finding in findings:
            record_id = str(finding.get("record_id") or "").strip()
            issue_type = str(finding.get("issue_type") or "").strip()
            if not record_id or not issue_type:
                continue
            record_type = str(finding.get("record_type") or "contract").strip().lower()
            if record_type not in {"contract", "customer"}:
                record_type = "contract"
            cursor.execute(
                """MERGE INTO score_ledger target USING (
                    SELECT %s AS team_id, %s AS record_id, %s AS record_type, %s AS issue_type
                ) source ON target.team_id = source.team_id
                    AND target.record_id = source.record_id
                    AND target.record_type = source.record_type
                    AND target.issue_type = source.issue_type
                WHEN NOT MATCHED THEN INSERT (team_id, record_id, record_type, issue_type, timestamp)
                    VALUES (source.team_id, source.record_id, source.record_type, source.issue_type, CURRENT_TIMESTAMP())""",
                (team_id, record_id, record_type, issue_type),
            )


def leaderboard_rows() -> list[dict[str, str]]:
    ensure_score_ledger_table()
    with snowflake_cursor() as cursor:
        cursor.execute("SELECT team_id, record_id, record_type, issue_type FROM score_ledger")
        return [
            {"team_id": row[0], "record_id": row[1], "record_type": row[2], "issue_type": row[3]}
            for row in cursor.fetchall()
        ]
