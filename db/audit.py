from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from .connection import snowflake_cursor


def save_chat_error(
    *, session_id: str, team_id: str, participant_id: str, message: str,
    error_type: str, error_message: str, stage: str = "unknown",
    retry_count: int = 0, response_status: str = "unknown",
    output_length: int = 0, parse_position: int = -1,
) -> None:
    """Store sanitized runtime failures that occur before normal turn logging."""
    with snowflake_cursor() as cursor:
        cursor.execute("""CREATE TABLE IF NOT EXISTS chat_error_logs (
            error_id VARCHAR PRIMARY KEY, session_id VARCHAR, team_id VARCHAR,
            participant_id VARCHAR, user_message VARCHAR, error_type VARCHAR,
            error_message VARCHAR, stage VARCHAR, retry_count NUMBER,
            response_status VARCHAR, output_length NUMBER, parse_position NUMBER,
            created_at TIMESTAMP_NTZ NOT NULL
        )""")
        for column, definition in (
            ("stage", "VARCHAR"), ("retry_count", "NUMBER"),
            ("response_status", "VARCHAR"), ("output_length", "NUMBER"),
            ("parse_position", "NUMBER"),
        ):
            cursor.execute(f"ALTER TABLE chat_error_logs ADD COLUMN IF NOT EXISTS {column} {definition}")
        cursor.execute(
            """INSERT INTO chat_error_logs
            (error_id, session_id, team_id, participant_id, user_message,
             error_type, error_message, stage, retry_count, response_status,
             output_length, parse_position, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP())""",
            (
                str(uuid.uuid4()), session_id, team_id, participant_id,
                message[:1000], error_type[:120], error_message[:1000],
                stage[:40], retry_count, response_status[:80], output_length,
                parse_position,
            ),
        )


DEFAULT_TEAMS = [
    ("TEAM_AI", "Team 1"),
    ("DATA_1", "Team 2"),
    ("DATA_2", "Team 3"),
    ("DATA_3", "Team 4"),
    ("DATA_4", "Team 5"),
    ("DATA_5", "Team 6"),
    ("PAPER_1", "Team 7"),
    ("PAPER_2", "Team 8"),
    ("DEMO_X", "Data X"),
    ("DEMO_Y", "Paper X"),
    ("DEMO_Z", "AI X"),
]

TEAM_GROUPS = {
    "TEAM_AI": "DATA",
    "DATA_1": "DATA",
    "DATA_2": "DATA",
    "DATA_3": "PAPER",
    "DATA_4": "DATA",
    "DATA_5": "AI",
    "PAPER_1": "DATA",
    "PAPER_2": "DATA",
    "DEMO_X": "DATA",
    "DEMO_Y": "PAPER",
    "DEMO_Z": "AI",
}


def ensure_audit_tables() -> None:
    with snowflake_cursor() as cursor:
        cursor.execute("""CREATE TABLE IF NOT EXISTS audit_sessions (
            session_id VARCHAR PRIMARY KEY, team_id VARCHAR NOT NULL,
            participant_id VARCHAR, participant_name VARCHAR,
            started_at TIMESTAMP_NTZ NOT NULL,
            ended_at TIMESTAMP_NTZ, status VARCHAR NOT NULL
        )""")
        cursor.execute("ALTER TABLE audit_sessions ADD COLUMN IF NOT EXISTS participant_name VARCHAR")
        cursor.execute("""CREATE TABLE IF NOT EXISTS team_chat_logs (
            turn_id VARCHAR PRIMARY KEY, session_id VARCHAR NOT NULL,
            team_id VARCHAR NOT NULL, participant_id VARCHAR,
            turn_no INTEGER NOT NULL, speaker VARCHAR NOT NULL,
            message VARCHAR, activity_json VARIANT, state_before_json VARIANT,
            state_after_json VARIANT, created_at TIMESTAMP_NTZ NOT NULL
        )""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS scored_chat_details (
            score_detail_id VARCHAR PRIMARY KEY, turn_id VARCHAR NOT NULL,
            session_id VARCHAR NOT NULL, team_id VARCHAR NOT NULL,
            participant_id VARCHAR, issue_type VARCHAR, entity_type VARCHAR,
            entity_id VARCHAR, customer_id VARCHAR, contract_id VARCHAR,
            finding_status VARCHAR, score_delta NUMBER, finding_json VARIANT,
            created_at TIMESTAMP_NTZ NOT NULL
        )""")
        cursor.execute("""CREATE TABLE IF NOT EXISTS team_score_ledger (
            team_id VARCHAR NOT NULL, issue_type VARCHAR NOT NULL,
            contract_count NUMBER DEFAULT 0, customer_count NUMBER DEFAULT 0,
            finding_count NUMBER DEFAULT 0, total_score NUMBER DEFAULT 0,
            updated_at TIMESTAMP_NTZ NOT NULL,
            PRIMARY KEY (team_id, issue_type)
        )""")


def seed_default_teams() -> None:
    with snowflake_cursor() as cursor:
        cursor.execute("ALTER TABLE team ADD COLUMN IF NOT EXISTS team_group VARCHAR DEFAULT 'DATA'")
        for team_id, team_name in DEFAULT_TEAMS:
            cursor.execute(
                """MERGE INTO team target USING (SELECT %s AS team_id, %s AS team_name, %s AS team_group)
                source ON target.team_id = source.team_id
                WHEN MATCHED THEN UPDATE SET team_name = source.team_name, team_group = source.team_group
                WHEN NOT MATCHED THEN INSERT (team_id, team_name, team_group, created_at)
                VALUES (source.team_id, source.team_name, source.team_group, CURRENT_TIMESTAMP())""",
                (team_id, team_name, TEAM_GROUPS.get(team_id, "DATA")),
            )


def create_session(*, session_id: str, team_id: str, participant_id: str,
                   participant_name: str = "") -> None:
    with snowflake_cursor() as cursor:
        cursor.execute(
            """INSERT INTO audit_sessions
            (session_id, team_id, participant_id, participant_name, started_at, status)
            VALUES (%s,%s,%s,%s,CURRENT_TIMESTAMP(),'active')""",
            (session_id, team_id, participant_id, participant_name),
        )


def clear_audit_data() -> None:
    """Delete audit activity while preserving the team registry."""
    with snowflake_cursor() as cursor:
        cursor.execute("DELETE FROM scored_chat_details")
        cursor.execute("DELETE FROM team_chat_logs")
        cursor.execute("DELETE FROM audit_sessions")
        cursor.execute("DELETE FROM team_score_ledger")


def save_chat_turn(
    *, team_id: str, session_id: str, participant_id: str, turn_no: int,
    speaker: str, message: str, activity: dict[str, Any],
    state_before: dict[str, Any], state_after: dict[str, Any],
) -> str:
    turn_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with snowflake_cursor() as cursor:
        cursor.execute(
            """INSERT INTO team_chat_logs
            (turn_id, session_id, team_id, participant_id, turn_no, speaker,
             message, activity_json, state_before_json, state_after_json, created_at)
            SELECT %s,%s,%s,%s,%s,%s,%s,PARSE_JSON(%s),PARSE_JSON(%s),PARSE_JSON(%s),%s""",
            (turn_id, session_id, team_id, participant_id, turn_no, speaker,
             message, json.dumps(activity), json.dumps(state_before),
             json.dumps(state_after), now),
        )
    return turn_id


def save_turn_bundle(
    *, team_id: str, session_id: str, participant_id: str, turn_no: int,
    user_message: str, assistant_message: str, activity: dict[str, Any],
    state_before: dict[str, Any], state_after: dict[str, Any],
    findings: list[dict[str, Any]],
) -> str:
    """Persist both chat messages and score details in one Snowflake transaction."""
    assistant_turn_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with snowflake_cursor() as cursor:
        for turn_id, speaker, message in (
            (str(uuid.uuid4()), "auditor", user_message),
            (assistant_turn_id, "mikael", assistant_message),
        ):
            cursor.execute(
                """INSERT INTO team_chat_logs
                (turn_id, session_id, team_id, participant_id, turn_no, speaker,
                 message, activity_json, state_before_json, state_after_json, created_at)
                SELECT %s,%s,%s,%s,%s,%s,%s,PARSE_JSON(%s),PARSE_JSON(%s),PARSE_JSON(%s),%s""",
                (turn_id, session_id, team_id, participant_id, turn_no, speaker,
                 message, json.dumps(activity), json.dumps(state_before),
                 json.dumps(state_after), now),
            )
        for finding in findings:
            cursor.execute(
                """INSERT INTO scored_chat_details
                (score_detail_id, turn_id, session_id, team_id, participant_id,
                 issue_type, entity_type, entity_id, customer_id, contract_id,
                 finding_status, score_delta, finding_json, created_at)
                SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,PARSE_JSON(%s),%s""",
                (str(uuid.uuid4()), assistant_turn_id, session_id, team_id,
                 participant_id, finding.get("issue_type"), finding.get("record_type"),
                 finding.get("record_id"), finding.get("customer_id"),
                 finding.get("contract_id"), finding.get("status"),
                 1 if finding.get("status") == "new_score" else 0,
                 json.dumps(finding), now),
            )
        cursor.execute(
            """MERGE INTO team_score_ledger target USING (
                SELECT team_id, issue_type,
                    COUNT(DISTINCT contract_id), COUNT(DISTINCT customer_id),
                    COUNT(*), COUNT(DISTINCT COALESCE(contract_id, customer_id, entity_id))
                FROM scored_chat_details
                WHERE team_id = %s AND finding_status = 'new_score'
                GROUP BY team_id, issue_type
            ) source ON target.team_id = source.COLUMN1 AND target.issue_type = source.COLUMN2
            WHEN MATCHED THEN UPDATE SET
                contract_count = source.COLUMN3, customer_count = source.COLUMN4,
                finding_count = source.COLUMN5, total_score = source.COLUMN6,
                updated_at = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
                (team_id, issue_type, contract_count, customer_count, finding_count, total_score, updated_at)
                VALUES (source.COLUMN1, source.COLUMN2, source.COLUMN3, source.COLUMN4,
                        source.COLUMN5, source.COLUMN6, CURRENT_TIMESTAMP())""",
            (team_id,),
        )
    return assistant_turn_id


def save_score_details(*, turn_id: str, session_id: str, team_id: str,
                       participant_id: str, findings: list[dict[str, Any]]) -> None:
    if not findings:
        return
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with snowflake_cursor() as cursor:
        for finding in findings:
            cursor.execute(
                """INSERT INTO scored_chat_details
                (score_detail_id, turn_id, session_id, team_id, participant_id,
                 issue_type, entity_type, entity_id, customer_id, contract_id,
                 finding_status, score_delta, finding_json, created_at)
                SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,PARSE_JSON(%s),%s""",
                (str(uuid.uuid4()), turn_id, session_id, team_id, participant_id,
                 finding.get("issue_type"), finding.get("record_type"),
                 finding.get("record_id"), finding.get("customer_id"),
                 finding.get("contract_id"), finding.get("status"),
                 1 if finding.get("status") == "new_score" else 0,
                json.dumps(finding), now),
            )
        cursor.execute(
            """MERGE INTO team_score_ledger target USING (
                SELECT team_id, issue_type,
                    COUNT(DISTINCT contract_id), COUNT(DISTINCT customer_id),
                    COUNT(*), COUNT(DISTINCT COALESCE(contract_id, customer_id, entity_id))
                FROM scored_chat_details
                WHERE team_id = %s AND finding_status = 'new_score'
                GROUP BY team_id, issue_type
            ) source ON target.team_id = source.team_id AND target.issue_type = source.issue_type
            WHEN MATCHED THEN UPDATE SET
                contract_count = source.COLUMN3, customer_count = source.COLUMN4,
                finding_count = source.COLUMN5, total_score = source.COLUMN6,
                updated_at = CURRENT_TIMESTAMP()
            WHEN NOT MATCHED THEN INSERT
                (team_id, issue_type, contract_count, customer_count, finding_count, total_score, updated_at)
                VALUES (source.COLUMN1, source.COLUMN2, source.COLUMN3, source.COLUMN4,
                        source.COLUMN5, source.COLUMN6, CURRENT_TIMESTAMP())""",
            (team_id,),
        )


def leaderboard_rows() -> list[dict[str, str]]:
    with snowflake_cursor() as cursor:
        cursor.execute(
            """SELECT team_id, record_id, record_type, issue_type,
            CASE WHEN row_number = 1 THEN team_score ELSE 0 END
            FROM (
                SELECT team_id,
                    COALESCE(contract_id, customer_id, entity_id) AS record_id,
                    CASE WHEN contract_id IS NULL THEN 'customer' ELSE 'contract' END AS record_type,
                    issue_type,
                    SUM(COALESCE(score_delta, 0)) OVER (PARTITION BY team_id) AS team_score,
                    ROW_NUMBER() OVER (PARTITION BY team_id ORDER BY created_at, score_detail_id) AS row_number
                FROM scored_chat_details
                WHERE finding_status = 'new_score'
            )"""
        )
        return [
            {
                "team_id": row[0],
                "record_id": row[1],
                "record_type": row[2],
                "issue_type": row[3],
                "score_delta": float(row[4] or 0),
            }
            for row in cursor.fetchall()
        ]


def participant_score_rows() -> list[dict[str, Any]]:
    with snowflake_cursor() as cursor:
        cursor.execute(
            """SELECT s.participant_id, COALESCE(s.participant_name, s.participant_id),
            d.team_id, SUM(COALESCE(d.score_delta, 0))
            FROM scored_chat_details d
            LEFT JOIN audit_sessions s ON s.session_id = d.session_id
            WHERE d.finding_status = 'new_score'
            GROUP BY s.participant_id, s.participant_name, d.team_id
            ORDER BY d.team_id, 2"""
        )
        return [
            {
                "participant_id": row[0],
                "participant_name": row[1],
                "team_id": row[2],
                "score": float(row[3] or 0),
            }
            for row in cursor.fetchall()
        ]


def recent_activity_logs(limit: int = 100) -> list[dict[str, Any]]:
    with snowflake_cursor() as cursor:
        cursor.execute(
            """SELECT created_at, team_id, participant_id, speaker, message,
            activity_json FROM team_chat_logs ORDER BY created_at DESC LIMIT %s""",
            (limit,),
        )
        return [
            {"created_at": row[0], "team_id": row[1], "participant_id": row[2],
             "speaker": row[3], "message": row[4], "activity": row[5]}
            for row in cursor.fetchall()
        ]


def recent_error_logs(limit: int = 100) -> list[dict[str, Any]]:
    with snowflake_cursor() as cursor:
        cursor.execute(
            """SELECT created_at, session_id, team_id, participant_id, error_type,
            error_message, stage, retry_count FROM chat_error_logs
            ORDER BY created_at DESC LIMIT %s""",
            (limit,),
        )
        return [
            {"created_at": row[0], "session_id": row[1], "team_id": row[2],
             "participant_id": row[3], "error_type": row[4],
             "error_message": row[5], "stage": row[6], "retry_count": row[7]}
            for row in cursor.fetchall()
        ]


def load_team_score_ledger(team_id: str) -> dict[str, dict[str, Any]]:
    """Load previously scored team findings in the runtime ledger format."""
    ledger: dict[str, dict[str, Any]] = {}
    with snowflake_cursor() as cursor:
        cursor.execute(
            """SELECT issue_type, contract_id, customer_id, finding_json
            FROM scored_chat_details
            WHERE team_id = %s AND finding_status = 'new_score'""",
            (team_id,),
        )
        for issue_type, contract_id, customer_id, finding_json in cursor.fetchall():
            if isinstance(finding_json, str):
                try:
                    finding = json.loads(finding_json)
                except json.JSONDecodeError:
                    finding = {}
            else:
                finding = finding_json if isinstance(finding_json, dict) else {}
            issue_key = str(finding.get("issue_key") or issue_type or "").strip()
            if issue_key and "_" not in issue_key:
                issue_key = issue_key.upper().replace(" ", "_").replace("-", "_")
            record_id = str(contract_id or finding.get("record_id") or "").strip()
            if not issue_key or not record_id:
                continue
            ledger[f"{team_id}::{record_id}::{issue_key}"] = {
                "record_id": record_id,
                "contract_id": contract_id,
                "customer_id": customer_id,
                "issue_type": issue_type,
                "issue_key": issue_key,
                "status": "new_score",
                "team": team_id,
            }
    return ledger
