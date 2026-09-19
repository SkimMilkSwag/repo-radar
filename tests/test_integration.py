"""End-to-end integration test: raw payload -> normalize -> storage -> history.

Drives the full pipeline the way a real run does — feeding fixture payloads
through :func:`normalize_repo` into :func:`upsert_repos`, then reading back
both the current-state table and the snapshot history to confirm every stage
agrees.  The network layer stays monkeypatched, so this still runs offline;
what it covers is everything *between* the API response and the reader
queries (normalize + storage), plus the CLI's ``history`` view of the result.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from reporadar import main as cli  # noqa: E402
from reporadar.normalize import REPO_COLUMNS, normalize_repo, summarize  # noqa: E402
from reporadar.storage import (  # noqa: E402
    connect,
    get_history,
    get_repos,
    upsert_repos,
)

# Fixture: the raw payload as GitHub would return it (incl. fields the
# normalizer must drop or flatten).
RAW_REPO = {
    "id": 101,
    "node_id": "R_kgDO1",
    "full_name": "some-org/some-repo",
    "name": "some-repo",
    "owner": {"login": "some-org", "id": 7},
    "private": False,
    "html_url": "https://github.com/some-org/some-repo",
    "description": "A demo repo",
    "fork": False,
    "language": "Python",
    "stargazers_count": 42,
    "watchers_count": 42,
    "forks_count": 7,
    "open_issues_count": 3,
    "archived": False,
    "has_issues": True,
    "created_at": "2025-01-02T03:04:05Z",
    "updated_at": "2026-09-10T00:00:00Z",
}

# A second repo, used to make the db hold more than one row.
RAW_REPO_B = {
    "full_name": "some-org/tools",
    "name": "tools",
    "owner": {"login": "some-org"},
    "description": None,
    "language": "Go",
    "stargazers_count": 5,
    "forks_count": 1,
    "open_issues_count": 0,
    "archived": False,
}


def test_raw_payload_round_trips_through_storage_and_history(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "integration.db")
    conn = connect(db)

    # --- sync 1: the fixture payloads go through normalize -> upsert ---
    repos = [normalize_repo(RAW_REPO), normalize_repo(RAW_REPO_B)]
    written = upsert_repos(conn, repos, synced_at="2026-09-14T00:00:00Z")
    assert written == 2

    # current-state table reflects the latest values for both repos
    rows = get_repos(conn)
    by_name = {r["full_name"]: r for r in rows}
    assert set(by_name) == {"some-org/some-repo", "some-org/tools"}
    demo = by_name["some-org/some-repo"]
    assert demo["stars"] == 42
    assert demo["forks"] == 7
    assert demo["owner"] == "some-org"  # nested owner.login flattened
    assert demo["last_synced_at"] == "2026-09-14T00:00:00Z"

    # every normalized row carries exactly the tracked columns
    for r in repos:
        assert set(r) == set(REPO_COLUMNS)

    # --- sync 2: same repos, stars moved -> history must show both ---
    bumped = {**RAW_REPO, "stargazers_count": 50, "forks_count": 9}
    upsert_repos(
        conn, [normalize_repo(bumped), normalize_repo(RAW_REPO_B)],
        synced_at="2026-09-16T00:00:00Z",
    )

    # current state now shows the bumped numbers, still one row per repo
    rows = get_repos(conn)
    assert len(rows) == 2
    by_name = {r["full_name"]: r for r in rows}
    assert by_name["some-org/some-repo"]["stars"] == 50
    assert by_name["some-org/some-repo"]["forks"] == 9

    # history kept both snapshots, newest first, with the delta recoverable
    history = get_history(conn, "some-org/some-repo")
    assert [h["synced_at"] for h in history] == [
        "2026-09-16T00:00:00Z", "2026-09-14T00:00:00Z",
    ]
    newest, oldest = history[0], history[-1]
    assert newest["stars"] - oldest["stars"] == 8   # +8 stars between syncs
    assert newest["forks"] - oldest["forks"] == 2

    # per-repo history for the other repo: one row per sync (it was
    # included in both syncs), with no movement between them
    tools_history = get_history(conn, "some-org/tools")
    assert len(tools_history) == 2
    assert {h["stars"] for h in tools_history} == {5}
    conn.close()

    # --- the CLI reads the same db and agrees with the storage layer ---
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["history", "some-org/some-repo", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    # newest snapshot first, and the +8 delta is visible in the CLI view too
    assert out.splitlines()[0].startswith("2026-09-16T00:00:00Z")
    assert "+8" in out

    # summarize() over the normalized rows matches what storage stored
    s = summarize(repos)
    assert s["repo_count"] == 2
    assert s["total_stars"] == 47  # 42 + 5 (state at sync 1)
    assert s["languages"] == {"Python": 1, "Go": 1}
