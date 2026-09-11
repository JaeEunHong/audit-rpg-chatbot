from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .connection import snowflake_cursor


def ensure_team_table() -> None:
    with snowflake_cursor() as cursor:
        cursor.execute(
            """CREATE TABLE IF NOT EXISTS team (
                team_id VARCHAR NOT NULL,
                team_name VARCHAR NOT NULL UNIQUE,
                team_group VARCHAR NOT NULL DEFAULT 'DATA',
                created_at TIMESTAMP_NTZ NOT NULL
            )"""
        )


def ensure_participant_table() -> None:
    with snowflake_cursor() as cursor:
        cursor.execute("""CREATE TABLE IF NOT EXISTS participant (
            participant_id VARCHAR NOT NULL, participant_name VARCHAR NOT NULL UNIQUE,
            created_at TIMESTAMP_NTZ NOT NULL)""")


def list_participants() -> list[str]:
    ensure_participant_table()
    with snowflake_cursor() as cursor:
        cursor.execute("SELECT participant_name FROM participant ORDER BY participant_name")
        return [row[0] for row in cursor.fetchall()]


def save_participants(names: list[str]) -> None:
    ensure_participant_table()
    cleaned = []
    seen = set()
    for name in names:
        value = str(name or "").strip()
        if value and value.casefold() not in seen:
            cleaned.append(value)
            seen.add(value.casefold())
    with snowflake_cursor() as cursor:
        existing = {row[0] for row in cursor.execute("SELECT participant_name FROM participant").fetchall()}
        for name in cleaned:
            cursor.execute(
                "MERGE INTO participant target USING (SELECT %s AS participant_name) source "
                "ON LOWER(target.participant_name) = LOWER(source.participant_name) "
                "WHEN NOT MATCHED THEN INSERT (participant_id, participant_name, created_at) "
                "VALUES (%s, source.participant_name, CURRENT_TIMESTAMP())",
                (name, str(uuid.uuid4())),
            )
        for name in existing - set(cleaned):
            cursor.execute("DELETE FROM participant WHERE participant_name = %s", (name,))


def list_teams() -> list[dict[str, str]]:
    ensure_team_table()
    with snowflake_cursor() as cursor:
        cursor.execute("SELECT team_id, team_name, COALESCE(team_group, 'DATA') FROM team ORDER BY team_name")
        return [{"team_id": row[0], "team_name": row[1], "group": row[2]} for row in cursor.fetchall()]


def create_team(team_name: str) -> dict[str, str]:
    name = team_name.strip()
    if not name:
        raise ValueError("Enter a team name.")
    ensure_team_table()
    team = {
        "team_id": str(uuid.uuid4()),
        "team_name": name,
        "group": "DATA",
        "created_at": datetime.now(timezone.utc).replace(tzinfo=None),
    }
    with snowflake_cursor() as cursor:
        cursor.execute("SELECT 1 FROM team WHERE LOWER(team_name) = LOWER(%s) LIMIT 1", (name,))
        if cursor.fetchone():
            raise ValueError("That team name is already in use.")
        cursor.execute(
            "INSERT INTO team (team_id, team_name, team_group, created_at) VALUES (%s, %s, %s, %s)",
            (team["team_id"], team["team_name"], team["group"], team["created_at"]),
        )
    return {"team_id": team["team_id"], "team_name": team["team_name"]}


def save_teams(teams: list[dict[str, str]]) -> None:
    """Replace the active team registry with the facilitator's edited rows."""
    ensure_team_table()
    cleaned = []
    seen_names = set()
    for row in teams:
        team_name = str(row.get("team_name") or "").strip()
        if not team_name:
            continue
        name_key = team_name.casefold()
        if name_key in seen_names:
            raise ValueError(f"Duplicate team name: {team_name}")
        seen_names.add(name_key)
        group = str(row.get("group") or "DATA").upper()
        if group not in {"DATA", "PAPER", "AI"}:
            raise ValueError(f"Invalid team group: {group}")
        cleaned.append((str(row.get("team_id") or uuid.uuid4()), team_name, group))
    with snowflake_cursor() as cursor:
        existing = {row[0] for row in cursor.execute("SELECT team_id FROM team").fetchall()}
        incoming = {row[0] for row in cleaned}
        for team_id, team_name, group in cleaned:
            if team_id in existing:
                cursor.execute(
                    "UPDATE team SET team_name = %s, team_group = %s WHERE team_id = %s",
                    (team_name, group, team_id),
                )
            else:
                cursor.execute(
                    "INSERT INTO team (team_id, team_name, team_group, created_at) "
                    "VALUES (%s, %s, %s, CURRENT_TIMESTAMP())",
                    (team_id, team_name, group),
                )
        for team_id in existing - incoming:
            cursor.execute("DELETE FROM team WHERE team_id = %s", (team_id,))
