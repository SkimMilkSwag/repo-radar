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


def _seed_history(db: str, stars_over_time: dict[str, int]):
    """Seed one repo's snapshots at fixed dates (deterministic tests)."""
    from reporadar.normalize import normalize_repo
    from reporadar.storage import connect, upsert_repos

    conn = connect(db)
    for synced_at, stars in stars_over_time.items():
        upsert_repos(
            conn, [normalize_repo({**RAW, "stargazers_count": stars})],
            synced_at=synced_at,
        )
    conn.close()


def test_history_since_filters_old_snapshots(tmp_path, monkeypatch, capsys):
    from datetime import datetime, timezone

    _seed_history(db := str(tmp_path / "r.db"), {
        "2026-09-01T00:00:00Z": 100,
        "2026-09-08T00:00:00Z": 101,
        "2026-09-15T00:00:00Z": 103,
    })

    monkeypatch.chdir(tmp_path)
    # unfiltered: all three snapshots
    rc = cli.main(["history", "org/alpha", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    lines = [l for l in out.splitlines() if l.startswith("2026-")]
    assert len(lines) == 3

    # the window is relative to *now*, so compute the largest day count
    # that still excludes the newest seeded snapshot (2026-09-15): any
    # smaller window is empty no matter when this test runs
    cutoff = datetime(2026, 9, 16, tzinfo=timezone.utc)
    days_empty = max((datetime.now(timezone.utc) - cutoff).days - 1, 1)
    rc = cli.main(["history", "org/alpha", "--since", str(days_empty),
                   "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    # nothing in the window: the empty message names it, and no delta is
    # computed against a filtered-out row
    assert "no history" in out and f"{days_empty}d ago" in out


def test_history_since_no_snapshots_in_window(tmp_path, monkeypatch, capsys):
    from reporadar.normalize import normalize_repo
    from reporadar.storage import connect, upsert_repos

    db = str(tmp_path / "r.db")
    conn = connect(db)
    upsert_repos(conn, [normalize_repo(RAW)], synced_at="2020-01-01T00:00:00Z")
    conn.close()

    monkeypatch.chdir(tmp_path)
    rc = cli.main(["history", "org/alpha", "--since", "3", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    # empty window: the message names the window, and there are no rows
    assert "no history" in out
    assert "3d ago" in out


def test_history_since_json_mode(tmp_path, monkeypatch, capsys):
    import json

    from datetime import datetime, timezone

    _seed_history(db := str(tmp_path / "r.db"), {
        "2026-09-01T00:00:00Z": 100,
        "2026-09-15T00:00:00Z": 103,
    })

    monkeypatch.chdir(tmp_path)
    # window covering only the newest snapshot (relative to now)
    days = max(
        (datetime.now(timezone.utc) - datetime(2026, 9, 15, tzinfo=timezone.utc)).days + 1,
        1,
    )
    rc = cli.main(["history", "org/alpha", "--since", str(days), "--json",
                   "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    # only the in-window snapshot, and with no previous row returned the
    # deltas are null rather than a bogus +3 against the filtered-out row
    assert len(payload) == 1
    assert payload[0]["synced_at"] == "2026-09-15T00:00:00Z"
    assert payload[0]["delta_stars"] is None


def test_report_prints_language_table_with_deltas(
    tmp_path, monkeypatch, capsys
):
    from reporadar.normalize import normalize_repo
    from reporadar.storage import connect, upsert_repos

    db = str(tmp_path / "r.db")
    conn = connect(db)
    # day 1: alpha at 100 stars/10 forks (Python), beta at 5 stars/1 fork (Go)
    upsert_repos(
        conn, [normalize_repo(RAW), normalize_repo(RAW_B)],
        synced_at="2026-09-15T00:00:00Z",
    )
    # day 2: both repos re-synced in one pass (a full snapshot); alpha
    # gained 3 stars, beta unchanged.
    upsert_repos(
        conn,
        [normalize_repo({**RAW, "stargazers_count": 103}), normalize_repo(RAW_B)],
        synced_at="2026-09-16T00:00:00Z",
    )
    conn.close()

    monkeypatch.chdir(tmp_path)
    rc = cli.main(["report", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    # header, language rows, and totals
    assert "# repo-radar report" in out
    assert "| Python |" in out and "| Go |" in out and "Total" in out
    # Python grew +3 stars; forks flat (alpha's only delta)
    py_line = next(l for l in out.splitlines() if l.startswith("| Python"))
    assert "+3" in py_line
    assert " 10 " in py_line.replace("+3", "")  # current fork count, no delta col
    # totals row reflects both repos: 108 stars (+3), 11 forks (flat)
    total_line = next(l for l in out.splitlines() if l.startswith("| **Total"))
    assert "108" in total_line and "+3" in total_line


def test_report_first_snapshot_has_no_deltas(tmp_path, monkeypatch, capsys):
    from reporadar.normalize import normalize_repo
    from reporadar.storage import connect, upsert_repos

    db = str(tmp_path / "r.db")
    conn = connect(db)
    upsert_repos(
        conn, [normalize_repo(RAW), normalize_repo(RAW_B)],
        synced_at="2026-09-15T00:00:00Z",
    )
    conn.close()

    monkeypatch.chdir(tmp_path)
    rc = cli.main(["report", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    # single snapshot: delta columns render 0 (equal to baseline), and
    # nothing has moved, so no +N anywhere in a data row
    lines = [l for l in out.splitlines()
             if l and not l.startswith(("#", "_", "-", "|"))]
    for line in lines:
        if line.startswith(("LANG", "Total")):
            assert "+" not in line


def test_top_json_emits_repo_list(tmp_path, monkeypatch, capsys):
    import json

    from reporadar.normalize import normalize_repo
    from reporadar.storage import connect, upsert_repos

    db = str(tmp_path / "r.db")
    conn = connect(db)
    upsert_repos(conn, [normalize_repo(RAW), normalize_repo(RAW_B)])
    conn.close()

    monkeypatch.chdir(tmp_path)
    rc = cli.main(["top", "--json", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    # a JSON array of repo objects, most-starred first
    assert isinstance(payload, list) and len(payload) == 2
    assert payload[0]["full_name"] == "org/alpha"
    assert payload[0]["stars"] == 100
    assert payload[1]["language"] == "Go"


def test_history_json_includes_deltas(tmp_path, monkeypatch, capsys):
    import json

    from reporadar.normalize import normalize_repo
    from reporadar.storage import connect, upsert_repos

    db = str(tmp_path / "r.db")
    conn = connect(db)
    upsert_repos(conn, [normalize_repo(RAW)], synced_at="2026-09-15T00:00:00Z")
    upsert_repos(
        conn,
        [normalize_repo({**RAW, "stargazers_count": 103})],
        synced_at="2026-09-16T00:00:00Z",
    )
    conn.close()

    monkeypatch.chdir(tmp_path)
    rc = cli.main(["history", "org/alpha", "--json", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    # newest first; each entry carries deltas vs the previous snapshot
    assert len(payload) == 2
    assert payload[0]["synced_at"] == "2026-09-16T00:00:00Z"
    assert payload[0]["delta_stars"] == 3
    # the oldest snapshot has no previous one: null deltas
    assert payload[1]["delta_stars"] is None
    assert payload[1]["delta_forks"] is None


def test_sync_merges_multiple_owners_into_one_db(tmp_path, monkeypatch, capsys):
    from reporadar.storage import connect, get_repos

    db = str(tmp_path / "r.db")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "reporadar.main.list_repos",
        lambda owner, token=None: [RAW] if owner == "org" else [RAW_B],
    )

    rc = cli.main(["sync", "org", "org2", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    # both owners synced in the one run, each with its own summary line
    assert "synced 1 repo(s) for org:" in out
    assert "synced 1 repo(s) for org2:" in out
    # everything merged into the same db
    conn = connect(db)
    rows = get_repos(conn)
    conn.close()
    assert {r["full_name"] for r in rows} == {"org/alpha", "org/beta"}


def test_sync_partial_owner_failure_returns_1(tmp_path, monkeypatch, capsys):
    from reporadar.client import APIError
    from reporadar.storage import connect, get_repos

    db = str(tmp_path / "r.db")
    monkeypatch.chdir(tmp_path)

    def flaky(owner, token=None):
        if owner == "org-bad":
            raise APIError(404, "Not Found")
        return [RAW]

    monkeypatch.setattr("reporadar.main.list_repos", flaky)
    rc = cli.main(["sync", "org-good", "org-bad", "--db", db])
    err = capsys.readouterr().err
    assert rc == 1
    assert "org-bad" in err  # the failing owner is named
    # the good owner's repos still landed
    conn = connect(db)
    rows = get_repos(conn)
    conn.close()
    assert [r["full_name"] for r in rows] == ["org/alpha"]


def test_report_empty_db(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "r.db")
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["report", "--db", db])
    assert rc == 0
    assert "no repos" in capsys.readouterr().out


def _seed_db(db: str):
    """Seed a db with alpha (100 stars, has description) and beta."""
    from reporadar.normalize import normalize_repo
    from reporadar.storage import connect, upsert_repos

    conn = connect(db)
    upsert_repos(
        conn,
        [normalize_repo(RAW), normalize_repo(RAW_B)],
        synced_at="2026-09-15T00:00:00Z",
    )
    conn.close()


def test_export_json_lists_all_repos(tmp_path, monkeypatch, capsys):
    import json

    db = str(tmp_path / "r.db")
    _seed_db(db)
    monkeypatch.chdir(tmp_path)

    rc = cli.main(["export", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    # every tracked repo, most-starred first, with the full export columns
    assert [r["full_name"] for r in payload] == ["org/alpha", "org/beta"]
    for row in payload:
        assert set(row) == set(cli.EXPORT_CSV_COLUMNS)
    alpha = next(r for r in payload if r["full_name"] == "org/alpha")
    assert alpha["stars"] == 100
    assert alpha["language"] == "Python"
    # archived comes back as a lowercase string, not 0/None noise
    assert alpha["archived"] == "false"


def test_export_csv_file_matches_json(tmp_path, monkeypatch, capsys):
    import csv

    db = str(tmp_path / "r.db")
    csv_path = str(tmp_path / "snapshot.csv")
    _seed_db(db)
    monkeypatch.chdir(tmp_path)

    rc = cli.main(["export", "--csv", csv_path, "--db", db])
    err = capsys.readouterr().err
    assert rc == 0
    # the file is written, and stdout stays clean (no JSON mixed in) so
    # only the stderr note reports the write — with the path as given
    assert f"exported 2 repo(s) to {csv_path}" in err

    with open(csv_path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["full_name"] for r in rows] == ["org/alpha", "org/beta"]
    # NULL description renders as the empty string, not 'None'
    beta = next(r for r in rows if r["full_name"] == "org/beta")
    assert beta["description"] == ""
    # booleans use the same true/false form as the JSON export
    alpha = next(r for r in rows if r["full_name"] == "org/alpha")
    assert alpha["archived"] == "false"
    assert alpha["stars"] == "100"


def test_export_csv_to_stdout(tmp_path, monkeypatch, capsys):
    import csv
    import io

    db = str(tmp_path / "r.db")
    _seed_db(db)
    monkeypatch.chdir(tmp_path)

    rc = cli.main(["export", "--csv", "-", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    # stdout is pure CSV when --csv - is used (no JSON mixed in)
    header, *data = out.splitlines()
    assert header.startswith("full_name")
    parsed = list(csv.DictReader(io.StringIO(out)))
    assert len(parsed) == 2


def test_export_csv_bad_path_returns_1(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "r.db")
    _seed_db(db)
    monkeypatch.chdir(tmp_path)

    rc = cli.main(["export", "--csv", "no-such-dir/out.csv", "--db", db])
    assert rc == 1
    err = capsys.readouterr().err
    assert "error:" in err and "out.csv" in err


def test_export_empty_db_returns_1(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "r.db")
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["export", "--db", db])
    assert rc == 1
    assert "no repos" in capsys.readouterr().out


def test_search_prints_matches_with_description(tmp_path, monkeypatch, capsys):
    import json

    from reporadar.normalize import normalize_repo
    from reporadar.storage import connect, upsert_repos

    db = str(tmp_path / "r.db")
    conn = connect(db)
    upsert_repos(
        conn,
        [
            {**normalize_repo(RAW), "description": "A fast deployment toolchain for CI pipelines"},
            normalize_repo(RAW_B),
        ],
        synced_at="2026-09-15T00:00:00Z",
    )
    conn.close()

    monkeypatch.chdir(tmp_path)
    rc = cli.main(["search", "pipelines", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    # the match is listed in the top-style table, with its description
    assert "org/alpha" in out
    assert "deployment toolchain" in out
    # beta has no description mentioning the term: not listed
    assert "org/beta" not in out


def test_search_json_emits_repo_rows(tmp_path, monkeypatch, capsys):
    import json

    from reporadar.normalize import normalize_repo
    from reporadar.storage import connect, upsert_repos

    db = str(tmp_path / "r.db")
    conn = connect(db)
    upsert_repos(
        conn,
        [
            {**normalize_repo(RAW), "description": "A fast deployment toolchain for CI pipelines"},
            normalize_repo(RAW_B),
        ],
        synced_at="2026-09-15T00:00:00Z",
    )
    conn.close()

    monkeypatch.chdir(tmp_path)
    rc = cli.main(["search", "pipelines", "--json", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert len(payload) == 1
    assert payload[0]["full_name"] == "org/alpha"


def test_search_no_match_prints_message(tmp_path, monkeypatch, capsys):
    from reporadar.normalize import normalize_repo
    from reporadar.storage import connect, upsert_repos

    db = str(tmp_path / "r.db")
    conn = connect(db)
    upsert_repos(conn, [normalize_repo(RAW)], synced_at="2026-09-15T00:00:00Z")
    conn.close()

    monkeypatch.chdir(tmp_path)
    rc = cli.main(["search", "zorp", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no repo matches" in out


def test_search_empty_db_returns_1(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "r.db")
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["search", "anything", "--db", db])
    assert rc == 1
    assert "no repos" in capsys.readouterr().out
