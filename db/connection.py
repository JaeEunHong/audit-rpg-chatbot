from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Iterator

from audit_rpg import load_env

_connection_state = threading.local()


def _connection_kwargs() -> dict[str, str]:
    load_env()
    mapping = {
        "account": "SNOWFLAKE_ACCOUNT",
        "user": "SNOWFLAKE_USER",
        "password": "SNOWFLAKE_PASSWORD",
        "warehouse": "SNOWFLAKE_WAREHOUSE",
        "database": "SNOWFLAKE_DATABASE",
        "schema": "SNOWFLAKE_SCHEMA",
        "role": "SNOWFLAKE_ROLE",
    }
    return {key: os.environ[name] for key, name in mapping.items() if os.environ.get(name)}


def _snowflake_connection():
    connection = getattr(_connection_state, "connection", None)
    if connection is not None:
        return connection
    try:
        import snowflake.connector
    except ImportError as exc:
        raise RuntimeError("Snowflake persistence requires snowflake-connector-python.") from exc

    kwargs = _connection_kwargs()
    required = {"account", "user", "password"}
    missing = sorted(required - kwargs.keys())
    if missing:
        raise RuntimeError("Snowflake configuration is missing: " + ", ".join(missing))
    connection = snowflake.connector.connect(**kwargs)
    _connection_state.connection = connection
    return connection


@contextmanager
def snowflake_cursor() -> Iterator[object]:
    connection = _snowflake_connection()
    cursor = connection.cursor()
    try:
        yield cursor
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
