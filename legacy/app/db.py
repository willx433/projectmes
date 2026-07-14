"""SQLite access + schema init. Raw sqlite3 — internal single-writer tool, no ORM needed.
# ponytail: one schema.sql applied idempotently; add numbered migration files if/when
# the schema must change in place. Greenfield doesn't need a migration framework yet.
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config

_SCHEMA = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 1


def now() -> str:
    """UTC ISO timestamp, JB2-compatible (yyyy-MM-ddTHH:mm:ssZ)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(_SCHEMA.read_text())
        row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        if not row or row["v"] is None:
            conn.execute(
                "INSERT INTO schema_version(version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, now()),
            )
        conn.commit()
    finally:
        conn.close()


def query(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


def one(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()
