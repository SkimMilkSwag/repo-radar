"""Tests for ``sync --note`` (persisted snapshot annotation).

All offline via monkeypatch of ``list_repos``: deterministic raw payloads,
throwaway db in a temp dir.
"""

import os
import sqlite3
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


def test_sync_note_persists_to_history(tmp_path, monkeypatch):
    db = str(tmp_path / "note.db")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "reporadar.main.list_repos", lambda owner, token=None, include_private=False: [_raw("alpha", stars=10)]
    )
    rc = cli.main(["sync", "org", "--db", db, "--note", "post-rewrite"])
    assert rc == 0

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT note FROM history ORDER BY id").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0]["note"] == "post-rewrite"


def test_sync_without_note_leaves_column_null(tmp_path, monkeypatch):
    db = str(tmp_path / "note.db")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "reporadar.main.list_repos", lambda owner, token=None, include_private=False: [_raw("alpha", stars=10)]
    )
    rc = cli.main(["sync", "org", "--db", db])
    assert rc == 0

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT note FROM history LIMIT 1").fetchone()
    conn.close()
    assert row["note"] is None


def test_history_shows_note_on_matching_line(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "note.db")
    monkeypatch.chdir(tmp_path)

    state = {"stars": 10}

    def fake(owner, token=None, include_private=False):
        return [_raw("alpha", stars=state["stars"])]

    monkeypatch.setattr("reporadar.main.list_repos", fake)

    # baseline sync with a note, then an unannotated follow-up sync
    rc = cli.main(["sync", "org", "--db", db, "--note", "post-rewrite"])
    assert rc == 0
    capsys.readouterr()
    state["stars"] = 12
    rc = cli.main(["sync", "org", "--db", db])
    assert rc == 0
    capsys.readouterr()

    # history must show the note next to the annotated snapshot's line only
    rc = cli.main(["history", "org/alpha", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    lines = out.splitlines()
    note_lines = [l for l in lines if "[post-rewrite]" in l]
    assert len(note_lines) == 1, f"expected exactly one annotated line, got: {note_lines}"
    # the annotated line is the OLDER snapshot (stars 10), not the new one
    assert "10" in note_lines[0]
    # the unannotated newer line has no bracket suffix
    plain = [l for l in out.splitlines() if "stars" in l and "[" not in l]
    assert len(plain) == 1 and "12" in plain[0]


def test_history_json_includes_note_field(tmp_path, monkeypatch, capsys):
    import json

    db = str(tmp_path / "note.db")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "reporadar.main.list_repos", lambda owner, token=None, include_private=False: [_raw("alpha", stars=10)]
    )
    rc = cli.main(["sync", "org", "--db", db, "--note", "v2 release"])
    assert rc == 0
    capsys.readouterr()

    rc = cli.main(["history", "org/alpha", "--db", db, "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    payload = json.loads(out)
    assert len(payload) == 1
    assert payload[0]["note"] == "v2 release"
