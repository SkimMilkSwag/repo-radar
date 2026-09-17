"""CLI tests for repo-radar: sync / top / history, offline via monkeypatch.

Each test runs the CLI in a temp dir with a throwaway db so state never
leaks between tests or onto the working tree.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from reporadar import main as cli  # noqa: E402
from reporadar.client import list_repos  # noqa: E402

RAW = {
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
RAW_B = {
    "full_name": "org/beta",
    "name": "beta",
    "owner": {"login": "org"},
    "language": "Go",
    "stargazers_count": 5,
    "forks_count": 1,
}


def test_sync_writes_db_and_prints_summary(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "r.db")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("reporadar.main.list_repos",
                       lambda owner, token=None: [RAW, RAW_B])

    rc = cli.main(["sync", "org", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    assert "2 repo(s)" in out
    assert "105 stars" in out  # 100 + 5

    from reporadar.storage import connect, get_repos
    conn = connect(db)
    rows = get_repos(conn)
    conn.close()
    assert [r["full_name"] for r in rows] == ["org/alpha", "org/beta"]


def test_sync_api_error_returns_1(tmp_path, monkeypatch, capsys):
    from reporadar.client import APIError

    db = str(tmp_path / "r.db")
    monkeypatch.chdir(tmp_path)

    def boom(owner, token=None):
        raise APIError(404, "Not Found")

    monkeypatch.setattr("reporadar.main.list_repos", boom)
    rc = cli.main(["sync", "org", "--db", db])
    assert rc == 1
    assert "error:" in capsys.readouterr().err


def test_top_prints_table_most_starred_first(tmp_path, monkeypatch, capsys):
    from reporadar.normalize import normalize_repo
    from reporadar.storage import connect, upsert_repos

    db = str(tmp_path / "r.db")
    conn = connect(db)
    upsert_repos(conn, [normalize_repo(RAW), normalize_repo(RAW_B)])
    conn.close()

    monkeypatch.chdir(tmp_path)
    rc = cli.main(["top", "5", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    # rows are ordered stars-desc; repo full name is the last field
    lines = [l for l in out.splitlines() if "org/" in l]
    assert lines[0].endswith("org/alpha")  # 100 stars first
    assert len(lines) == 2


def test_top_empty_db(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "r.db")
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["top", "--db", db])
    assert rc == 0
    assert "no repos" in capsys.readouterr().out


def test_history_shows_deltas(tmp_path, monkeypatch, capsys):
    from reporadar.normalize import normalize_repo
    from reporadar.storage import connect, upsert_repos

    db = str(tmp_path / "r.db")
    conn = connect(db)
    # day 1: 100 stars; day 2: 103 stars (+3)
    upsert_repos(conn, [normalize_repo(RAW)], synced_at="2026-09-15T00:00:00Z")
    upsert_repos(
        conn,
        [normalize_repo({**RAW, "stargazers_count": 103})],
        synced_at="2026-09-16T00:00:00Z",
    )
    conn.close()

    monkeypatch.chdir(tmp_path)
    rc = cli.main(["history", "org/alpha", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    # newest snapshot first, with the +3 delta vs the previous one
    assert out.splitlines()[0].startswith("2026-09-16T00:00:00Z")
    assert "+3" in out


def test_history_unknown_repo(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "r.db")
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["history", "nobody/nothing", "--db", db])
    assert rc == 0
    assert "no history" in capsys.readouterr().out
