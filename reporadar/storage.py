"""SQLite storage: current state per repo + timestamped snapshot history.

Two tables:

* ``repos`` — one row per repo full name, upserted on every sync so it
  always reflects the latest API response (``last_synced_at`` tracks when).
* ``history`` — append-only; one row per repo per sync, so star/fork/
  issue counts can be compared across snapshots.

The db file path is whatever the caller passes in; repo-radar's CLI
defaults to ``./repo_radar.db``.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .normalize import REPO_COLUMNS

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    full_name TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    owner TEXT,
    description TEXT,
    language TEXT,
    stars INTEGER NOT NULL DEFAULT 0,
    forks INTEGER NOT NULL DEFAULT 0,
    open_issues INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT,
    last_synced_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name TEXT NOT NULL,
    name TEXT NOT NULL,
    owner TEXT,
    language TEXT,
    stars INTEGER NOT NULL DEFAULT 0,
    forks INTEGER NOT NULL DEFAULT 0,
    open_issues INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT,
    synced_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_history_name_time ON history (full_name, synced_at);
"""


def utcnow() -> str:
    """Current UTC time as an ISO-8601 string (second precision)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: str) -> sqlite3.Connection:
    """Open (and initialize the schema of) the db at ``db_path``."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def upsert_repos(conn: sqlite3.Connection, repos: list[dict],
                 synced_at: str | None = None) -> int:
    """Upsert normalized rows into ``repos`` and append to ``history``.

    Returns the number of rows written.  A single transaction keeps the
    two tables consistent; a crash mid-sync can't leave history without
    state (or vice versa).
    """
    synced_at = synced_at or utcnow()
    cols = ", ".join(REPO_COLUMNS)
    placeholders = ", ".join("?" * len(REPO_COLUMNS))
    updates = ", ".join(
        f"{c}=excluded.{c}" for c in REPO_COLUMNS if c != "full_name"
    )
    stmt = (
        f"INSERT INTO repos ({cols}, last_synced_at) "
        f"VALUES ({placeholders}, ?) "
        f"ON CONFLICT(full_name) DO UPDATE SET {updates}, "
        f"last_synced_at=excluded.last_synced_at"
    )
    rows = [tuple(r.get(c) for c in REPO_COLUMNS) + (synced_at,)
            for r in repos]

    h_cols = ", ".join(
        c for c in REPO_COLUMNS if c != "description"
    )
    n_h = len(h_cols.split(", "))
    h_stmt = (f"INSERT INTO history ({h_cols}, synced_at) "
              f"VALUES ({', '.join(['?'] * (n_h + 1))})")
    h_rows = [tuple(r.get(c) for c in REPO_COLUMNS if c != "description") + (synced_at,)
              for r in repos]

    with conn:
        conn.executemany(stmt, rows)
        conn.executemany(h_stmt, h_rows)
    return len(rows)


def get_repos(conn: sqlite3.Connection) -> list[dict]:
    """Return the current state of every tracked repo, most stars first."""
    cursor = conn.execute(
        "SELECT * FROM repos ORDER BY stars DESC, full_name ASC"
    )
    return [dict(row) for row in cursor.fetchall()]


def get_history(conn: sqlite3.Connection, full_name: str | None = None,
                limit: int = 100) -> list[dict]:
    """Return historical snapshots (newest first), optionally one repo."""
    if full_name:
        cursor = conn.execute(
            "SELECT * FROM history WHERE full_name = ? "
            "ORDER BY synced_at DESC, id DESC LIMIT ?",
            (full_name, limit),
        )
    else:
        cursor = conn.execute(
            "SELECT * FROM history ORDER BY synced_at DESC, id DESC LIMIT ?",
            (limit,),
        )
    return [dict(row) for row in cursor.fetchall()]
