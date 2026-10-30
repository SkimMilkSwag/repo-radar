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
from typing import Any
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

CREATE VIRTUAL TABLE IF NOT EXISTS repo_search USING fts5(
    name, full_name UNINDEXED, description,
    content='repos', content_rowid='rowid'
);
"""


def _fts5_available(conn: sqlite3.Connection) -> bool:
    """True when this SQLite build ships FTS5 (every official build does)."""
    try:
        conn.execute("CREATE VIRTUAL TABLE __fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE __fts5_probe")
        return True
    except sqlite3.OperationalError:
        return False


def utcnow() -> str:
    """Current UTC time as an ISO-8601 string (second precision)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: str) -> sqlite3.Connection:
    """Open (and initialize the schema of) the db at ``db_path``."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # Dbs created before repo_search existed have a stale external-content
    # index: rebuild it from repos so search covers everything already stored.
    if _fts5_available(conn):
        conn.execute("INSERT INTO repo_search(repo_search) VALUES('rebuild')")
        conn.commit()
    return conn


def _reindex_all(conn: sqlite3.Connection) -> None:
    """Rebuild the whole FTS index from repos (cheap; called once per sync)."""
    conn.execute("INSERT INTO repo_search(repo_search) VALUES('rebuild')")


def upsert_repos(conn: sqlite3.Connection, repos: list[dict[str, Any]],
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
        if _fts5_available(conn):
            # one rebuild per sync keeps the index consistent with whatever
            # was just upserted (inserts and description edits alike)
            _reindex_all(conn)
    return len(rows)


def get_repos(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return the current state of every tracked repo, most stars first."""
    cursor = conn.execute(
        "SELECT * FROM repos ORDER BY stars DESC, full_name ASC"
    )
    return [dict(row) for row in cursor.fetchall()]


def search_repos(conn: sqlite3.Connection, term: str, limit: int = 20) -> list[dict[str, Any]]:
    """Full-text search over repo names and descriptions.

    Uses the FTS5 ``repo_search`` index when this SQLite build has it;
    otherwise falls back to LIKE scans so a minimal build still works.
    Results come back most-starred first, like every other view in
    repo-radar.  A multi-word term matches any word (OR), which is what
    makes it feel like search rather than exact-substring matching.
    """
    term = term.strip()
    if not term:
        return []
    if _fts5_available(conn):
        # quote each word as a bare FTS5 string to keep quotes/specials safe
        words = " OR ".join(
            '"{}"'.format(w.replace('"', '""')) for w in term.split()
        )
        cursor = conn.execute(
            "SELECT r.* FROM repo_search s "
            "JOIN repos r ON r.rowid = s.rowid "
            "WHERE repo_search MATCH ? "
            "ORDER BY r.stars DESC, r.full_name ASC LIMIT ?",
            (words, limit),
        )
    else:
        like = "%" + " %".join(f"%{w}%" for w in term.split()) + "%"
        cursor = conn.execute(
            "SELECT * FROM repos WHERE name LIKE ? OR description LIKE ? "
            "ORDER BY stars DESC, full_name ASC LIMIT ?",
            (like, like, limit),
        )
    return [dict(row) for row in cursor.fetchall()]


def get_history(conn: sqlite3.Connection, full_name: str | None = None,
                limit: int = 100, since: str | None = None) -> list[dict[str, Any]]:
    """Return historical snapshots (newest first), optionally one repo.

    ``since`` filters to snapshots taken on or after that ISO-8601 UTC
    timestamp, so a caller can window the deltas over the last N days.
    """
    if full_name:
        cursor = conn.execute(
            "SELECT * FROM history WHERE full_name = ? AND synced_at >= ? "
            "ORDER BY synced_at DESC, id DESC LIMIT ?",
            (full_name, since or "1970-01-01T00:00:00Z", limit),
        )
    else:
        cursor = conn.execute(
            "SELECT * FROM history WHERE synced_at >= ? "
            "ORDER BY synced_at DESC, id DESC LIMIT ?",
            (since or "1970-01-01T00:00:00Z", limit),
        )
    return [dict(row) for row in cursor.fetchall()]


def get_gone_repos(conn: sqlite3.Connection,
                   full_names: set[str]) -> list[dict[str, Any]]:
    """Return tracked repos whose ``full_name`` is not in ``full_names``.

    Used by the ``prune`` command after a sync: any repo that was 404 on
    re-fetch (deleted or renamed upstream) is "gone".  Rows come back in the
    same most-starred-first order as every other view, and each carries its
    full current-state row so the CLI can report stars/forks of what was
    removed, not just its name.
    """
    cursor = conn.execute(
        "SELECT * FROM repos ORDER BY stars DESC, full_name ASC"
    )
    return [dict(row) for row in cursor.fetchall()
            if row["full_name"] not in full_names]


def remove_repos(conn: sqlite3.Connection, full_names: list[str]) -> int:
    """Delete repos from the current-state table (and rebuild the FTS index).

    ``history`` rows are deliberately kept: a pruned repo's snapshots are
    still valid data for its lifetime, and deleting history would silently
    erase growth numbers the ``report`` command might still want.  Returns
    the number of repos removed (0 when nothing matched).

    Note: keeping history means "tracked at time T" (reconstructed from
    history via ``snapshots_at_or_before``) is deliberately *not* the same
    as "in the current-state table" — a pruned repo still counts as tracked
    in historical diffs.
    """
    if not full_names:
        return 0
    with conn:
        placeholders = ", ".join("?" * len(full_names))
        cursor = conn.execute(
            f"DELETE FROM repos WHERE full_name IN ({placeholders})",
            full_names,
        )
        if _fts5_available(conn):
            # the FTS index is external-content over repos: a plain DELETE
            # leaves stale entries behind unless the index is rebuilt.
            _reindex_all(conn)
    return cursor.rowcount


def get_latest_snapshots(conn: sqlite3.Connection,
                         limit: int = 100) -> list[dict[str, Any]]:
    """Latest stored snapshot per repo (newest sync time overall).

    For repos whose last sync predates the db's newest sync — e.g. a repo
    that dropped out of a later owner fetch and was pruned — the "latest"
    is whatever its final history row holds, so star growth up to pruning
    is still measurable.  Ordering: most recent first.
    """
    cursor = conn.execute(
        "SELECT h.* FROM history h WHERE h.id IN ("
        "  SELECT MAX(id) FROM history GROUP BY full_name) "
        "ORDER BY synced_at DESC, id DESC LIMIT ?",
        (limit,),
    )
    return [dict(row) for row in cursor.fetchall()]


def get_first_snapshots(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """First stored snapshot per repo — the baseline for all-time growth."""
    cursor = conn.execute(
        "SELECT * FROM history WHERE id IN "
        "(SELECT MIN(id) FROM history GROUP BY full_name)"
    )
    return [dict(row) for row in cursor.fetchall()]


def snapshots_at_or_before(conn: sqlite3.Connection,
                           synced_at: str) -> dict[str, dict[str, Any]]:
    """Latest snapshot of every repo at or before ``synced_at``.

    Returns ``{full_name: row}`` — the state of the db as it stood at that
    timestamp, reconstructed from history.  Repos with no snapshot yet are
    simply absent; callers diffing two snapshots treat absence as "not
    tracked then".
    """
    cursor = conn.execute(
        "SELECT h.* FROM history h WHERE h.id IN ("
        "  SELECT MAX(id) FROM history WHERE synced_at <= ? "
        "  GROUP BY full_name)",
        (synced_at,),
    )
    return {row["full_name"]: dict(row) for row in cursor.fetchall()}


def resolve_snapshot_time(conn: sqlite3.Connection,
                          name: str) -> str | None:
    """Resolve a snapshot reference to a stored ``synced_at`` timestamp.

    A reference is either an exact stored timestamp (``2026-10-29T03:00:00Z``)
    or a 1-based positional index into the distinct sync times, oldest first
    (``first``, ``last``, ``1``, ``2``, ...).  Returns ``None`` when the
    reference matches no stored snapshot.  The CLI lists valid references in
    its error message so users can discover them.
    """
    name = name.strip()
    if name.lower() in ("first", "last"):
        cursor = conn.execute(
            "SELECT DISTINCT synced_at FROM history ORDER BY synced_at"
        )
        times = [str(row["synced_at"]) for row in cursor.fetchall()]
        if not times:
            return None
        return times[0] if name.lower() == "first" else times[-1]
    if name.isdigit():
        idx = int(name) - 1
        cursor = conn.execute(
            "SELECT DISTINCT synced_at FROM history ORDER BY synced_at"
        )
        times = [str(row["synced_at"]) for row in cursor.fetchall()]
        return times[idx] if 0 <= idx < len(times) else None
    # exact timestamp match (compared case-sensitively: timestamps are
    # stored verbatim by utcnow())
    cursor = conn.execute(
        "SELECT 1 FROM history WHERE synced_at = ? LIMIT 1", (name,)
    )
    return name if cursor.fetchone() else None


def snapshots_between(conn: sqlite3.Connection,
                      after: str, at_or_before: str) -> dict[str, dict[str, Any]]:
    """Latest snapshot of every repo with ``after < synced_at <= at_or_before``.

    The state a two-snapshot diff shows *at NEW*: a repo survives only if it
    has a snapshot inside the (OLD, NEW] window.  A repo whose last row is
    stale — before OLD — is not "there" even though ``synced_at <= NEW`` would
    match it; that staleness is exactly what "removed" means in this context.
    """
    cursor = conn.execute(
        "SELECT h.* FROM history h WHERE h.id IN ("
        "  SELECT MAX(id) FROM history "
        "  WHERE synced_at > ? AND synced_at <= ? "
        "  GROUP BY full_name)",
        (after, at_or_before),
    )
    return {row["full_name"]: dict(row) for row in cursor.fetchall()}


def list_snapshot_times(conn: sqlite3.Connection) -> list[str]:
    """Distinct stored snapshot times, oldest first (for error messages)."""
    cursor = conn.execute(
        "SELECT DISTINCT synced_at FROM history ORDER BY synced_at"
    )
    return [row["synced_at"] for row in cursor.fetchall()]
