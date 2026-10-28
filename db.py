"""SQLite helpers.

* `connect(path)`      - open a new connection (used by background threads).
* `get_db()`           - the per-request connection stored on Flask's `g`.
* `init_db(path)`      - create all tables from schema.sql (safe to run repeatedly).

Every query in the project uses `?` placeholders (parameterised queries), which is
what protects the app against SQL injection. Never build SQL with f-strings.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from flask import current_app, g

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(path: str) -> sqlite3.Connection:
    """Open a connection with safe defaults (foreign keys on, WAL, busy timeout)."""
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(path: str) -> None:
    """Create the tables if they do not exist yet."""
    conn = connect(path)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.commit()
    finally:
        conn.close()


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


# ----- tiny query helpers (work with any connection) -------------------------
def query_all(conn, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def query_one(conn, sql: str, params: tuple = ()):
    return conn.execute(sql, params).fetchone()


def execute(conn, sql: str, params: tuple = ()) -> int:
    cur = conn.execute(sql, params)
    conn.commit()
    return cur.lastrowid


def to_json(value) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


def from_json(text, default=None):
    if text is None or text == "":
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default
