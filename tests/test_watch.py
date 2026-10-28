"""Tests for the ``watch`` subcommand (sync + diff vs previous snapshot).

All offline via monkeypatch of ``list_repos``: each test drives a throwaway
db in a temp dir and feeds it deterministic raw payloads.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from reporadar import main as cli  # noqa: E402


def _raw(name: str, owner: str = "org", stars: int = 0, forks: int = 0):
    return {
        "full_name": f"{owner}/{name}",
        "name": name,
        "owner": {"login": owner},
        "description": None,
        "language": "Python",
        "stargazers_count": stars,
        "forks_count": forks,
        "open_issues_count": 0,
        "archived": False,
        "created_at": "2025-06-01T00:00:00Z",
    }


def test_watch_first_run_reports_every_repo_as_new(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "w.db")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "reporadar.main.list_repos",
        lambda owner, token=None, include_private=False: [_raw("alpha", stars=10)],
    )

    rc = cli.main(["watch", "org", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    assert "new" in out and "org/alpha" in out
    assert "10" in out


def test_watch_unchanged_counts_are_silent(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "w.db")
    monkeypatch.chdir(tmp_path)

    def fake(owner, token=None, include_private=False):
        return [_raw("alpha", stars=10)]

    monkeypatch.setattr("reporadar.main.list_repos", fake)

    # first run: new
    rc = cli.main(["watch", "org", "--db", db])
    assert rc == 0
    capsys.readouterr()

    # second run with identical counts: nothing to report
    rc = cli.main(["watch", "org", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no changes since last snapshot" in out


def test_watch_reports_star_and_fork_deltas(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "w.db")
    monkeypatch.chdir(tmp_path)

    state = {"stars": 10, "forks": 2}

    def fake(owner, token=None, include_prime=False, **kw):
        return [_raw("alpha", stars=state["stars"], forks=state["forks"])]

    monkeypatch.setattr("reporadar.main.list_repos", fake)
    cli.main(["watch", "org", "--db", db])  # baseline: new
    capsys.readouterr()

    state["stars"] = 13
    state["forks"] = 5
    rc = cli.main(["watch", "org", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    # star and fork deltas are both reported for the changed repo
    assert "+3" in out and "+3" in out
    assert "org/alpha" in out


def test_watch_json_carries_old_new_and_deltas(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "w.db")
    monkeypatch.chdir(tmp_path)

    state = {"stars": 10, "forks": 2}

    def fake(owner, token=None, include_private=False, **kw):
        return [_raw("alpha", stars=state["stars"], forks=state["forks"])]

    monkeypatch.setattr("reporadar.main.list_repos", fake)
    cli.main(["watch", "org", "--db", db])  # baseline
    capsys.readouterr()

    state["stars"] = 12
    rc = cli.main(["watch", "org", "--json", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert len(payload) == 1
    entry = payload[0]
    assert entry["full_name"] == "org/alpha"
    assert entry["new"] is False
    assert entry["old_stars"] == 10 and entry["stars"] == 12
    assert entry["delta_stars"] == 2
    assert entry["old_forks"] == 2 and entry["forks"] == 2
    assert entry["delta_forks"] == 0


def test_watch_json_first_run_marks_new(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "w.db")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "reporadar.main.list_repos",
        lambda owner, token=None, include_private=False: [_raw("alpha", stars=7)],
    )

    rc = cli.main(["watch", "org", "--json", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload[0]["new"] is True
    assert payload[0]["old_stars"] == 0 and payload[0]["stars"] == 7


def test_watch_owner_failure_returns_1_and_names_owner(tmp_path, monkeypatch, capsys):
    from reporadar.client import APIError

    db = str(tmp_path / "w.db")
    monkeypatch.chdir(tmp_path)

    def flaky(owner, token=None, include_private=False):
        if owner == "org-bad":
            raise APIError(404, "Not Found")
        return [_raw("alpha", stars=1)]

    monkeypatch.setattr("reporadar.main.list_repos", flaky)
    rc = cli.main(["watch", "org-good", "org-bad", "--db", db])
    err = capsys.readouterr().err
    assert rc == 1
    assert "org-bad" in err
    # the good owner's repo still got upserted + reported as new
    conn = cli._open_db(db)
    rows = conn.execute("SELECT full_name FROM repos").fetchall()
    conn.close()
    assert [r["full_name"] for r in rows] == ["org/alpha"]


def test_watch_upserts_new_history_rows(tmp_path, monkeypatch):
    from reporadar.storage import get_history

    db = str(tmp_path / "w.db")
    monkeypatch.chdir(tmp_path)

    state = {"stars": 1}

    def fake(owner, token=None, include_private=False, **kw):
        return [_raw("alpha", stars=state["stars"])]

    monkeypatch.setattr("reporadar.main.list_repos", fake)
    cli.main(["watch", "org", "--db", db])
    state["stars"] = 2
    cli.main(["watch", "org", "--db", db])

    conn = cli._open_db(db)
    snaps = get_history(conn, "org/alpha")
    conn.close()
    # two snapshots appended (newest first), latest carries the new count
    assert len(snaps) == 2
    assert snaps[0]["stars"] == 2
    assert snaps[1]["stars"] == 1


def test_watch_multiple_owners_single_run(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "w.db")
    monkeypatch.chdir(tmp_path)

    def fake(owner, token=None, include_private=False):
        if owner == "org":
            return [_raw("alpha", stars=10)]
        return [_raw("beta", owner="org2", stars=3)]

    monkeypatch.setattr("reporadar.main.list_repos", fake)
    rc = cli.main(["watch", "org", "org2", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    # both owners' first-seen repos are reported as new in one run
    assert out.count("new") == 2
    assert "org/alpha" in out and "org2/beta" in out
