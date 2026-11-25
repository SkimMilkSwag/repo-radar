"""Tests for `report --json`: per-language totals + growth as a JSON document.

Seeds two snapshots of a small org (Python repo that grows, Go repo that
doesn't) and checks the payload mirrors the markdown table — same rows,
same order, same numbers — with null deltas where the table renders '—'.
"""

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from reporadar import main as cli  # noqa: E402
from reporadar.normalize import normalize_repo  # noqa: E402
from reporadar.storage import connect, upsert_repos  # noqa: E402


def _seed_two_snapshots(db: str) -> None:
    """Day 1: alpha (Python, 100/10) + beta (Go, 5/1); day 2: alpha at 103/12."""
    raw = {
        "full_name": "org/alpha",
        "name": "alpha",
        "owner": {"login": "org"},
        "description": None,
        "language": "Python",
        "stargazers_count": 100,
        "forks_count": 10,
        "open_issues_count": 2,
        "archived": False,
        "created_at": "2025-06-01T00:00:00Z",
    }
    raw_b = {
        "full_name": "org/beta",
        "name": "beta",
        "owner": {"login": "org"},
        "language": "Go",
        "stargazers_count": 5,
        "forks_count": 1,
    }
    conn = connect(db)
    upsert_repos(conn, [normalize_repo(raw), normalize_repo(raw_b)],
                 synced_at="2026-09-15T00:00:00Z")
    upsert_repos(
        conn,
        [normalize_repo({**raw, "stargazers_count": 103, "forks_count": 12}),
         normalize_repo(raw_b)],
        synced_at="2026-09-16T00:00:00Z",
    )
    conn.close()


def test_report_json_matches_table_numbers(tmp_path, capsys):
    db = str(tmp_path / "r.db")
    _seed_two_snapshots(db)

    rc = cli.main(["report", "--json", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)

    assert set(payload) == {"languages", "total"}
    # same row order as the markdown table: most-starred language first
    langs = [row["language"] for row in payload["languages"]]
    assert langs == ["Python", "Go"]
    py = payload["languages"][0]
    go = payload["languages"][1]
    # day-1 baseline: Python 100 stars / 10 forks, Go 5 / 1
    assert py == {
        "language": "Python", "repos": 1, "stars": 103, "delta_stars": 3,
        "forks": 12, "delta_forks": 2,
    }
    assert go == {
        "language": "Go", "repos": 1, "stars": 5, "delta_stars": 0,
        "forks": 1, "delta_forks": 0,
    }
    # totals cover every language and sum to the db's current state
    assert payload["total"] == {
        "repos": 2, "stars": 108, "delta_stars": 3,
        "forks": 13, "delta_forks": 2,
    }


def test_report_json_single_snapshot_has_zero_deltas(tmp_path, capsys):
    from reporadar.storage import connect, upsert_repos

    db = str(tmp_path / "r.db")
    conn = connect(db)
    upsert_repos(
        conn,
        [normalize_repo({
            "full_name": "org/solo", "name": "solo", "owner": {"login": "org"},
            "language": "Rust", "stargazers_count": 7, "forks_count": 1,
        })],
        synced_at="2026-09-15T00:00:00Z",
    )
    conn.close()

    rc = cli.main(["report", "--json", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    row = payload["languages"][0]
    # one snapshot: baseline equals current, so every delta is exactly 0
    assert (row["delta_stars"], row["delta_forks"]) == (0, 0)
    assert (payload["total"]["delta_stars"],
            payload["total"]["delta_forks"]) == (0, 0)


def test_report_json_language_added_after_baseline_has_null_deltas(tmp_path, capsys):
    """A language first tracked at the newest snapshot has no baseline.

    The markdown table renders its deltas as '—'; the JSON document must
    carry null for both delta fields of that row (and keep the totals'
    delta computed against the languages that *do* have a baseline).
    """
    db = str(tmp_path / "r.db")
    conn = connect(db)
    upsert_repos(
        conn,
        [normalize_repo({
            "full_name": "org/old", "name": "old", "owner": {"login": "org"},
            "language": "Python", "stargazers_count": 10, "forks_count": 2,
        })],
        synced_at="2026-09-15T00:00:00Z",
    )
    upsert_repos(
        conn,
        [normalize_repo({
            "full_name": "org/old", "name": "old", "owner": {"login": "org"},
            "language": "Python", "stargazers_count": 12, "forks_count": 2,
        }), normalize_repo({
            "full_name": "org/new", "name": "new", "owner": {"login": "org"},
            "language": "Go", "stargazers_count": 4, "forks_count": 0,
        })],
        synced_at="2026-09-16T00:00:00Z",
    )
    conn.close()

    rc = cli.main(["report", "--json", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    by_lang = {row["language"]: row for row in payload["languages"]}
    # Python existed at the baseline: real deltas
    assert (by_lang["Python"]["delta_stars"],
            by_lang["Python"]["delta_forks"]) == (2, 0)
    # Go was first seen at the newest snapshot: no baseline -> null
    assert by_lang["Go"]["delta_stars"] is None
    assert by_lang["Go"]["delta_forks"] is None
    # totals still measure growth of what was tracked at the baseline
    # (current 16 stars vs the 10-star baseline — includes Go's own count,
    # exactly like the markdown Total row)
    assert payload["total"]["delta_stars"] == 6
    assert payload["total"]["delta_forks"] == 0


def test_report_json_since_window_moves_baseline(tmp_path, capsys):
    """--since re-baselines: growth within the window only."""
    db = str(tmp_path / "r.db")
    conn = connect(db)
    upsert_repos(
        conn,
        [normalize_repo({
            "full_name": "org/alpha", "name": "alpha", "owner": {"login": "org"},
            "language": "Python", "stargazers_count": 100, "forks_count": 10,
        })],
        synced_at="2026-09-15T00:00:00Z",
    )
    upsert_repos(
        conn,
        [normalize_repo({
            "full_name": "org/alpha", "name": "alpha", "owner": {"login": "org"},
            "language": "Python", "stargazers_count": 103, "forks_count": 10,
        })],
        synced_at="2026-09-16T00:00:00Z",
    )
    conn.close()

    # a window that excludes the first snapshot (relative to now)
    days = max(
        (datetime.now(timezone.utc)
         - datetime(2026, 9, 16, tzinfo=timezone.utc)).days + 1,
        1,
    )
    rc = cli.main(["report", "--json", "--since", str(days), "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    py = payload["languages"][0]
    # baseline moved to the only in-window snapshot: nothing moved since
    assert (py["delta_stars"], py["delta_forks"]) == (0, 0)
    assert payload["total"]["delta_stars"] == 0

    # and the all-time report still sees the +3 growth
    rc = cli.main(["report", "--json", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    assert json.loads(out)["languages"][0]["delta_stars"] == 3


def test_report_json_language_ordering_and_no_description_field(tmp_path, capsys):
    """Order follows the table (most-starred first), rows carry no extras."""
    db = str(tmp_path / "r.db")
    conn = connect(db)
    upsert_repos(
        conn,
        [
            normalize_repo({
                "full_name": "org/zz", "name": "zz", "owner": {"login": "org"},
                "language": "Zig", "stargazers_count": 50, "forks_count": 5,
            }),
            normalize_repo({
                "full_name": "org/aa", "name": "aa", "owner": {"login": "org"},
                "language": "Assembly", "stargazers_count": 20, "forks_count": 1,
            }),
            normalize_repo({
                "full_name": "org/mm", "name": "mm", "owner": {"login": "org"},
                "language": "Assembly", "stargazers_count": 20, "forks_count": 2,
            }),
        ],
        synced_at="2026-09-15T00:00:00Z",
    )
    conn.close()

    rc = cli.main(["report", "--json", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    # most-starred language first; equal stars tie-break alphabetically
    assert [row["language"] for row in payload["languages"]] == [
        "Zig", "Assembly"
    ]
    asm = next(r for r in payload["languages"] if r["language"] == "Assembly")
    # both Assembly repos aggregate into one row
    assert asm["repos"] == 2 and asm["stars"] == 40
    # exactly the documented fields — no description/rowid leakage
    assert set(asm) == {"language", "repos", "stars",
                        "delta_stars", "forks", "delta_forks"}
