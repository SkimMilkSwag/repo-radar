"""Tests for the ``snapshots`` subcommand (list stored snapshot times)."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from reporadar import main as cli  # noqa: E402


def _raw(name: str, owner: str = "org", stars: int = 0):
    return {
        "full_name": f"{owner}/{name}",
        "name": name,
        "owner": {"login": owner},
        "description": None,
        "language": "Python",
        "stargazers_count": stars,
        "forks_count": 1,
        "open_issues_count": 0,
        "archived": False,
        "created_at": "2025-06-01T00:00:00Z",
    }


def _seed_two_snapshots(db: str):
    """Two syncs: one plain (2 repos), one annotated (3 repos)."""
    from reporadar.normalize import normalize_repo
    from reporadar.storage import connect, upsert_repos

    conn = connect(db)
    upsert_repos(
        conn,
        [normalize_repo(_raw("alpha", 1)), normalize_repo(_raw("beta", 2))],
        synced_at="2026-11-18T03:00:00Z",
    )
    upsert_repos(
        conn,
        [
            normalize_repo(_raw("alpha", 5)),
            normalize_repo(_raw("beta", 2)),
            normalize_repo(_raw("gamma", 9)),
        ],
        synced_at="2026-11-19T03:00:00Z",
        note="post-refactor",
    )
    conn.close()


def test_snapshots_lists_times_newest_first(tmp_path, capsys):
    db = str(tmp_path / "r.db")
    _seed_two_snapshots(db)

    rc = cli.main(["snapshots", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    lines = [l for l in out.splitlines() if l and not l.startswith("-")]
    # header first, then the two snapshot rows, newest timestamp first
    assert lines[0].startswith("SNAPSHOT")
    assert "2026-11-19T03:00:00Z" in lines[1]
    assert "2026-11-18T03:00:00Z" in lines[2]
    # the annotated run is identified by its note label
    assert "[post-refactor]" in lines[1]
    # the plain run carries no note bracket
    assert "[" not in lines[2]


def test_snapshots_repo_counts_reflect_tracked_set(tmp_path, capsys):
    db = str(tmp_path / "r.db")
    _seed_two_snapshots(db)

    rc = cli.main(["snapshots", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    # the first sync tracked 2 repos, the second 3 (gamma added)
    newest, oldest = None, None
    for line in out.splitlines():
        if line.startswith("2026-11-19"):
            newest = line
        elif line.startswith("2026-11-18"):
            oldest = line
    assert newest is not None and oldest is not None
    # repos column is right-aligned; the counts are the distinct full_names
    # present in each snapshot's history rows
    assert "3" in newest.split()
    assert "2" in oldest.split()


def test_snapshots_json_shape(tmp_path, capsys):
    db = str(tmp_path / "r.db")
    _seed_two_snapshots(db)

    rc = cli.main(["snapshots", "--json", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    entries = json.loads(out)
    # newest first, each entry carries synced_at / repos / note
    assert len(entries) == 2
    assert set(entries[0]) == {"synced_at", "repos", "note"}
    assert entries[0]["synced_at"] == "2026-11-19T03:00:00Z"
    assert entries[0]["repos"] == 3
    assert entries[0]["note"] == "post-refactor"
    assert entries[1]["synced_at"] == "2026-11-18T03:00:00Z"
    assert entries[1]["repos"] == 2
    # a run with no annotation reports note as null, not the empty string
    assert entries[1]["note"] is None


def test_snapshots_empty_db_returns_1(tmp_path, capsys):
    db = str(tmp_path / "r.db")

    rc = cli.main(["snapshots", "--db", db])
    out = capsys.readouterr().out
    assert rc == 1
    assert "no snapshots" in out


def test_snapshots_empty_db_json_returns_1(tmp_path, capsys):
    db = str(tmp_path / "r.db")

    rc = cli.main(["snapshots", "--json", "--db", db])
    out = capsys.readouterr().out
    assert rc == 1
    # the JSON view of an empty history is still a well-formed (empty) list
    assert json.loads(out) == []


def test_snapshots_single_repo_repeated_sync_counts(tmp_path, capsys):
    """A repo synced N times yields N snapshot rows; each counts that repo."""
    from reporadar.normalize import normalize_repo
    from reporadar.storage import connect, upsert_repos

    db = str(tmp_path / "r.db")
    conn = connect(db)
    for i in range(3):
        upsert_repos(
            conn,
            [normalize_repo(_raw("solo", i))],
            synced_at=f"2026-11-1{i}T03:00:00Z",
        )
    conn.close()

    rc = cli.main(["snapshots", "--json", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    entries = json.loads(out)
    # three distinct times, each covering exactly the one tracked repo
    assert len(entries) == 3
    assert all(e["repos"] == 1 for e in entries)
    assert all(e["note"] is None for e in entries)
