"""Tests for the ``export`` subcommand's positional DB path (parity with --db).

The positional is an alias for ``--db``: it names which db file to read.
Coverage here is the resolution logic and the end-to-end behavior through
the CLI — same rows and exit codes as the flag form, with the precedence
rule (explicit ``--db`` wins over a positional) pinned down explicitly.
"""
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from reporadar import main as cli  # noqa: E402


def _seed(db: str, synced_at: str = "2026-11-20T03:00:00Z") -> None:
    """Seed one repo into a db (deterministic content for assertions)."""
    from reporadar.normalize import normalize_repo
    from reporadar.storage import connect, upsert_repos

    raw = {
        "full_name": "org/alpha",
        "name": "alpha",
        "owner": {"login": "org"},
        "description": "positional db path marker",
        "language": "Python",
        "stargazers_count": 42,
        "forks_count": 3,
        "open_issues_count": 0,
        "archived": False,
        "created_at": "2025-06-01T00:00:00Z",
    }
    conn = connect(db)
    upsert_repos(conn, [normalize_repo(raw)], synced_at=synced_at)
    conn.close()


def test_resolve_db_precedence():
    """Unit-level pin: flag beats positional; neither -> default."""
    from reporadar.main import DEFAULT_DB, _resolve_db

    # explicit flag wins over a positional (the documented collision rule)
    assert _resolve_db("/a/flag.db", "/b/pos.db") == "/a/flag.db"
    # no flag: the positional is used
    assert _resolve_db(DEFAULT_DB, "/b/pos.db") == "/b/pos.db"
    # neither given: argparse's default stands in
    assert _resolve_db(DEFAULT_DB, None) == DEFAULT_DB
    # flag set to its literal default value with a positional: positional wins
    # (indistinguishable from "not given", which is the point of this rule)
    assert _resolve_db(DEFAULT_DB, "/c/pos.db") == "/c/pos.db"


def test_export_positional_db_matches_flag_form(tmp_path, monkeypatch, capsys):
    """Same db via positional vs --db: identical JSON payload, exit 0."""
    db = str(tmp_path / "r.db")
    _seed(db)
    monkeypatch.chdir(tmp_path)

    rc_pos = cli.main(["export", db])
    out_pos = capsys.readouterr().out
    assert rc_pos == 0

    rc_flag = cli.main(["export", "--db", db])
    out_flag = capsys.readouterr().out
    assert rc_flag == 0

    # both forms read the same db and print the same document
    assert json.loads(out_pos) == json.loads(out_flag)
    payload = json.loads(out_pos)
    assert len(payload) == 1
    assert payload[0]["full_name"] == "org/alpha"
    assert payload[0]["stars"] == 42


def test_export_positional_db_csv_file(tmp_path, monkeypatch, capsys):
    """Positional + --csv writes the CSV from the named db."""
    db = str(tmp_path / "r.db")
    out_path = str(tmp_path / "snapshot.csv")
    _seed(db)
    monkeypatch.chdir(tmp_path)

    rc = cli.main(["export", db, "--csv", out_path])
    err = capsys.readouterr().err
    assert rc == 0
    assert f"exported 1 repo(s) to {out_path}" in err

    with open(out_path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1
    assert rows[0]["full_name"] == "org/alpha"
    assert rows[0]["stars"] == "42"


def test_export_positional_and_flag_disagree_uses_flag(tmp_path, monkeypatch, capsys):
    """When both are passed with different values, the explicit --db wins."""
    db_a = str(tmp_path / "a.db")
    db_b = str(tmp_path / "b.db")
    _seed(db_a)  # a has content; b is empty (no file yet)
    monkeypatch.chdir(tmp_path)

    # positional points at the populated db, flag at the empty one:
    # the flag's db is what gets read -> empty db exit code
    rc = cli.main(["export", db_a, "--db", db_b])
    out = capsys.readouterr().out
    assert rc == 1
    assert "no repos" in out and db_b in out


def test_export_positional_empty_db_returns_1(tmp_path, monkeypatch, capsys):
    """Positional to an uncreated db path -> same empty-db contract as flag."""
    db = str(tmp_path / "never.db")
    monkeypatch.chdir(tmp_path)

    rc = cli.main(["export", db])
    out = capsys.readouterr().out
    assert rc == 1
    assert "no repos" in out


def test_export_positional_stdout_csv_is_pure(tmp_path, monkeypatch, capsys):
    """Positional + --csv -: stdout is the CSV document, nothing else."""
    db = str(tmp_path / "r.db")
    _seed(db)
    monkeypatch.chdir(tmp_path)

    rc = cli.main(["export", db, "--csv", "-"])
    out = capsys.readouterr().out
    assert rc == 0
    header, *data = out.splitlines()
    assert header.startswith("full_name")
    parsed = list(csv.DictReader(__import__("io").StringIO(out)))
    assert len(parsed) == 1
    assert parsed[0]["full_name"] == "org/alpha"


def test_export_without_db_arg_still_uses_default(tmp_path, monkeypatch, capsys):
    """No db argument at all: the default ./repo_radar.db is consulted."""
    default = str(tmp_path / "repo_radar.db")
    _seed(default)
    monkeypatch.chdir(tmp_path)

    rc = cli.main(["export"])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert payload[0]["full_name"] == "org/alpha"
