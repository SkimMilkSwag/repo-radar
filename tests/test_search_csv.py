"""Tests for `search --out` (CSV export of FTS search results)."""

import csv
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from reporadar import main as cli  # noqa: E402


def _seed_db(db: str):
    """Seed a db with two repos; alpha's description matches 'pipelines'."""
    from reporadar.normalize import normalize_repo
    from reporadar.storage import connect, upsert_repos

    raw = {
        "full_name": "org/alpha",
        "name": "alpha",
        "owner": {"login": "org"},
        "description": "A fast deployment toolchain for CI pipelines",
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
    conn.close()


def test_search_out_writes_csv_file(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "r.db")
    out_path = str(tmp_path / "matches.csv")
    _seed_db(db)
    monkeypatch.chdir(tmp_path)

    rc = cli.main(["search", "pipelines", "--out", out_path, "--db", db])
    err = capsys.readouterr().err
    assert rc == 0
    # the terminal table still prints; only the file write is on stderr
    assert f"exported 1 match(es) to {out_path}" in err

    with open(out_path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    # only the matching repo, same columns and value mapping as export
    assert len(rows) == 1
    row = rows[0]
    assert row["full_name"] == "org/alpha"
    assert set(row) == set(cli.EXPORT_CSV_COLUMNS)
    assert row["stars"] == "100"
    assert row["archived"] == "false"


def test_search_out_to_stdout_is_pure_csv(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "r.db")
    _seed_db(db)
    monkeypatch.chdir(tmp_path)

    rc = cli.main(["search", "pipelines", "--out", "-", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    # stdout is the CSV document itself — no terminal table mixed in
    header, *data = out.splitlines()
    assert header.startswith("full_name")
    parsed = list(csv.DictReader(io.StringIO(out)))
    assert len(parsed) == 1
    assert parsed[0]["full_name"] == "org/alpha"


def test_search_out_no_matches_writes_header_only(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "r.db")
    out_path = str(tmp_path / "matches.csv")
    _seed_db(db)
    monkeypatch.chdir(tmp_path)

    rc = cli.main(["search", "zorp", "--out", out_path, "--db", db])
    err = capsys.readouterr().err
    assert rc == 0
    with open(out_path, newline="") as fh:
        content = fh.read()
    # header row only — a no-match search still exports a valid, empty CSV
    assert content.splitlines()[0].startswith("full_name")
    assert len(content.splitlines()) == 1
    assert "exported 0 match(es)" in err


def test_search_out_bad_path_returns_1(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "r.db")
    _seed_db(db)
    monkeypatch.chdir(tmp_path)

    rc = cli.main(["search", "pipelines", "--out", "no-such-dir/out.csv",
                   "--db", db])
    assert rc == 1
    err = capsys.readouterr().err
    assert "error:" in err and "out.csv" in err


def test_search_out_empty_db_returns_1(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "r.db")
    out_path = str(tmp_path / "matches.csv")
    monkeypatch.chdir(tmp_path)

    rc = cli.main(["search", "anything", "--out", out_path, "--db", db])
    assert rc == 1
    assert "no repos" in capsys.readouterr().out
    # the empty-db check happens before any file is written
    assert not os.path.exists(out_path)


def test_search_out_honours_limit(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "r.db")
    out_path = str(tmp_path / "matches.csv")
    _seed_db(db)
    # add a second matching repo so --limit 1 has something to cut off
    from reporadar.normalize import normalize_repo
    from reporadar.storage import connect, upsert_repos

    conn = connect(db)
    upsert_repos(
        conn,
        [normalize_repo({
            "full_name": "org/gamma",
            "name": "gamma",
            "owner": {"login": "org"},
            "description": "Pipeline runner for CI pipelines",
            "language": "Go",
            "stargazers_count": 50,
            "forks_count": 3,
        })],
        synced_at="2026-09-15T00:00:00Z",
    )
    conn.close()
    monkeypatch.chdir(tmp_path)

    rc = cli.main(["search", "pipelines", "--limit", "1", "--out", out_path,
                   "--db", db])
    assert rc == 0
    with open(out_path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    # limit applies to the CSV exactly like it does to the table/JSON views
    assert len(rows) == 1
    assert rows[0]["full_name"] == "org/alpha"
