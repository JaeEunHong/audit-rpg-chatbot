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
        with snowflake_cursor() as cursor:
            cursor.execute("ALTER TABLE team ADD COLUMN IF NOT EXISTS team_group VARCHAR DEFAULT 'DATA'")


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
