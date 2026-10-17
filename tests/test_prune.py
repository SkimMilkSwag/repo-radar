"""CLI tests for the prune subcommand (offline via monkeypatched fetch)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from reporadar import main as cli  # noqa: E402
from reporadar.client import APIError  # noqa: E402
from reporadar.normalize import normalize_repo  # noqa: E402
from reporadar.storage import connect, get_gone_repos, remove_repos, upsert_repos  # noqa: E402


RAW_A = {
    "full_name": "org/alpha",
    "name": "alpha",
    "owner": {"login": "org"},
    "description": None,
    "language": "Python",
    "stargazers_count": 100,
    "forks_count": 10,
    "open_issues_count": 2,
    "archived": False,
}

RAW_B = {
    "full_name": "org/beta",
    "name": "beta",
    "owner": {"login": "org"},
    "language": "Go",
    "stargazers_count": 5,
    "forks_count": 1,
}


def _seed(db: str):
    """Seed alpha (100 stars) and beta (5 stars) into the current-state table."""
    conn = connect(db)
    upsert_repos(
        conn,
        [normalize_repo(RAW_A), normalize_repo(RAW_B)],
        synced_at="2026-09-15T00:00:00Z",
    )
    conn.close()


def test_prune_removes_gone_repo_and_keeps_others(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "r.db")
    _seed(db)
    monkeypatch.chdir(tmp_path)
    # upstream now only has beta: alpha was deleted on GitHub
    monkeypatch.setattr(
        "reporadar.main.list_repos", lambda owner, token=None: [RAW_B]
    )

    rc = cli.main(["prune", "org", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    # the gone repo is named with its last-seen stats before removal
    assert "gone: org/alpha (last seen 100 stars, 10 forks)" in out
    assert "pruned 1 repo(s)" in out

    conn = connect(db)
    rows = [r["full_name"] for r in get_gone_repos(conn, {"org/beta"})]
    all_rows = conn.execute("SELECT full_name FROM repos").fetchall()
    hist_count = conn.execute(
        "SELECT COUNT(*) FROM history WHERE full_name = 'org/alpha'"
    ).fetchone()[0]
    conn.close()
    # alpha is out of the current-state table, beta untouched...
    assert rows == []
    assert [r["full_name"] for r in all_rows] == ["org/beta"]
    # ...but its history rows survive (valid data for its lifetime)
    assert hist_count == 1


def test_prune_nothing_gone_is_a_noop(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "r.db")
    _seed(db)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "reporadar.main.list_repos", lambda owner, token=None: [RAW_A, RAW_B]
    )

    rc = cli.main(["prune", "org", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    assert "pruned 0 repo(s)" in out
    assert "still on GitHub" in out

    conn = connect(db)
    n = conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0]
    conn.close()
    assert n == 2


def test_prune_dry_run_lists_but_does_not_remove(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "r.db")
    _seed(db)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "reporadar.main.list_repos", lambda owner, token=None: [RAW_A]
    )

    rc = cli.main(["prune", "org", "--dry-run", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    assert "gone: org/beta (last seen 5 stars, 1 forks)" in out
    assert "dry run; nothing removed" in out

    conn = connect(db)
    n = conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0]
    conn.close()
    # nothing was actually deleted
    assert n == 2


def test_prune_api_error_aborts_before_removal(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "r.db")
    _seed(db)
    monkeypatch.chdir(tmp_path)

    def boom(owner, token=None):
        raise APIError(403, "rate limited")

    monkeypatch.setattr("reporadar.main.list_repos", boom)
    rc = cli.main(["prune", "org", "--db", db])
    err = capsys.readouterr().err
    assert rc == 1
    # a failed fetch can't prove a repo is gone: nothing was pruned
    assert "error:" in err and "403" in err
    conn = connect(db)
    n = conn.execute("SELECT COUNT(*) FROM repos").fetchone()[0]
    conn.close()
    assert n == 2


def test_prune_multiple_owners_union(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "r.db")
    # seed a repo from a second owner too: org2/gamma
    _seed(db)
    gamma = {**RAW_B, "full_name": "org2/gamma", "name": "gamma"}
    conn = connect(db)
    upsert_repos(conn, [normalize_repo(gamma)], synced_at="2026-09-15T00:00:00Z")
    conn.close()
    monkeypatch.chdir(tmp_path)

    def fetch(owner, token=None):
        # org only lists beta now; org2 still has gamma
        if owner == "org":
            return [RAW_B]
        return [{**RAW_B, "full_name": "org2/gamma", "name": "gamma"}]

    monkeypatch.setattr("reporadar.main.list_repos", fetch)
    rc = cli.main(["prune", "org", "org2", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    # alpha (org) is gone; gamma (org2) survived the union check
    assert "gone: org/alpha" in out
    assert "pruned 1 repo(s)" in out
    conn = connect(db)
    names = [r["full_name"] for r in conn.execute(
        "SELECT full_name FROM repos ORDER BY full_name")]
    conn.close()
    assert names == ["org/beta", "org2/gamma"]


def test_prune_empty_db_returns_1(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "r.db")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "reporadar.main.list_repos", lambda owner, token=None: [RAW_A]
    )
    rc = cli.main(["prune", "org", "--db", db])
    out = capsys.readouterr().out
    assert rc == 1
    assert "no repos" in out


def test_get_gone_repos_orders_most_starred_first(tmp_path):
    from datetime import datetime

    db = str(tmp_path / "r.db")
    conn = connect(db)
    # three repos with very different star counts; none are "seen"
    upsert_repos(
        conn,
        [
            normalize_repo(RAW_A),                       # 100 stars
            normalize_repo(RAW_B),                       # 5 stars
            normalize_repo({**RAW_B, "full_name": "org/zeta", "name": "zeta",
                            "stargazers_count": 50}),    # 50 stars
        ],
        synced_at="2026-09-15T00:00:00Z",
    )
    gone = get_gone_repos(conn, set())
    conn.close()
    assert [r["full_name"] for r in gone] == ["org/alpha", "org/zeta", "org/beta"]


def test_remove_repos_returns_count_and_keeps_history(tmp_path):
    db = str(tmp_path / "r.db")
    conn = connect(db)
    upsert_repos(
        conn, [normalize_repo(RAW_A), normalize_repo(RAW_B)],
        synced_at="2026-09-15T00:00:00Z",
    )
    removed = remove_repos(conn, ["org/alpha"])
    names = [r["full_name"] for r in conn.execute(
        "SELECT full_name FROM repos ORDER BY full_name")]
    hist_alpha = conn.execute(
        "SELECT COUNT(*) FROM history WHERE full_name = 'org/alpha'"
    ).fetchone()[0]
    # removing an unknown repo is a clean no-op, not an error
    removed_unknown = remove_repos(conn, ["org/nope"])
    conn.close()
    assert removed == 1
    assert removed_unknown == 0
    assert names == ["org/beta"]
    assert hist_alpha == 1


def test_prune_stale_fts_index_after_remove(tmp_path):
    # after pruning, a search for the pruned repo's description must not
    # resurrect it (the FTS index is external-content over repos).
    db = str(tmp_path / "r.db")
    conn = connect(db)
    upsert_repos(
        conn,
        [
            {**normalize_repo(RAW_A), "description": "unique alpha marker"},
            normalize_repo(RAW_B),
        ],
        synced_at="2026-09-15T00:00:00Z",
    )
    remove_repos(conn, ["org/alpha"])
    from reporadar.storage import search_repos

    rows = search_repos(conn, "unique alpha marker")
    conn.close()
    assert [r["full_name"] for r in rows] == []
