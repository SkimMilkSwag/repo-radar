"""Tests for repo-radar's core: client, normalize, storage.

The network layer is monkeypatched so the suite runs offline; SQLite
tests use a temp db file per test.
"""

import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from reporadar.client import (  # noqa: E402
    APIError,
    fetch_json,
    list_repos,
    next_url,
)
from reporadar.normalize import REPO_COLUMNS, normalize_repo, summarize  # noqa: E402
from reporadar.storage import connect, get_history, get_repos, upsert_repos  # noqa: E402

RAW_REPO = {
    "full_name": "some-org/some-repo",
    "name": "some-repo",
    "owner": {"login": "some-org"},
    "description": "A demo repo",
    "language": "Python",
    "stargazers_count": 42,
    "forks_count": 7,
    "open_issues_count": 3,
    "archived": False,
    "created_at": "2025-01-02T03:04:05Z",
}


def test_normalize_repo_maps_fields():
    row = normalize_repo(RAW_REPO)
    assert row["full_name"] == "some-org/some-repo"
    assert row["owner"] == "some-org"
    assert row["stars"] == 42
    assert row["forks"] == 7
    assert row["archived"] is False
    assert set(row) == set(REPO_COLUMNS)


def test_normalize_repo_defaults_for_missing_fields():
    row = normalize_repo({"full_name": "o/r", "name": "r"})
    assert row["stars"] == 0
    assert row["forks"] == 0
    assert row["language"] is None
    assert row["archived"] is False


def test_summarize_counts_and_language_breakdown():
    repos = [
        normalize_repo(RAW_REPO),  # Python
        normalize_repo({**RAW_REPO, "full_name": "o/other",
                       "name": "other", "stargazers_count": 1}),  # Python
        normalize_repo({"full_name": "o/scripts", "name": "scripts"}),  # no language
    ]
    s = summarize(repos)
    assert s["repo_count"] == 3
    assert s["total_stars"] == 43
    assert list(s["languages"]) == ["Python", "None"]
    assert s["languages"]["Python"] == 2


def test_next_url_parses_link_header():
    headers = {
        "Link": '<https://api.github.com/users/o/repos?page=2>; rel="next", '
                '<https://api.github.com/users/o/repos?page=5>; rel="last"'
    }
    assert next_url(headers) == "https://api.github.com/users/o/repos?page=2"
    assert next_url({"Link": ""}) is None
    assert next_url(None) is None


def test_fetch_json_ok(monkeypatch):
    class FakeResp:
        def __init__(self, body: bytes) -> None:
            self.body = body

        def read(self) -> bytes:
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        "reporadar.client.urllib.request.urlopen",
        lambda req: FakeResp(json.dumps({"ok": 1}).encode()),
    )
    assert fetch_json("https://example.invalid/x") == {"ok": 1}


def test_fetch_json_raises_api_error(monkeypatch):
    import urllib.error

    exc = urllib.error.HTTPError(
        "url", 404, "not found", None, __import__("io").BytesIO(b'{"message":"404"}')
    )
    monkeypatch.setattr(
        "reporadar.client.urllib.request.urlopen",
        lambda req: (_ for _ in ()).throw(exc),
    )
    try:
        fetch_json("https://example.invalid/x")
        assert False, "expected APIError"
    except APIError as e:
        assert e.status == 404


def test_list_repos_paginates(monkeypatch):
    class FakeResp:
        def __init__(self, body: bytes, headers: dict) -> None:
            self.body = body
            self.headers = headers

        def read(self) -> bytes:
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    calls = []

    def fake_urlopen(req):
        calls.append(req.full_url)
        if req.full_url.endswith("page=1") or "per_page=100" in req.full_url:
            return FakeResp(
                json.dumps([RAW_REPO]).encode(),
                {"Link": '<https://api.github.com/x?page=2>; rel="next"'},
            )
        return FakeResp(json.dumps([]).encode(), {})

    monkeypatch.setattr("reporadar.client.urllib.request.urlopen", fake_urlopen)
    repos = list_repos("some-org")
    assert [r["full_name"] for r in repos] == ["some-org/some-repo"]
    assert len(calls) == 2  # page 1, then the rel="next" URL


def _tmp_db(tmp_path):
    return str(tmp_path / "test.db")


def test_upsert_and_get_repos(tmp_path):
    conn = connect(_tmp_db(tmp_path))
    n = upsert_repos(conn, [normalize_repo(RAW_REPO)])
    assert n == 1
    rows = get_repos(conn)
    assert len(rows) == 1
    assert rows[0]["full_name"] == "some-org/some-repo"
    assert rows[0]["stars"] == 42
    assert rows[0]["last_synced_at"] is not None

    # second sync upserts (no duplicate PK) and appends to history
    upsert_repos(conn, [normalize_repo({**RAW_REPO, "stargazers_count": 43})])
    rows = get_repos(conn)
    assert len(rows) == 1
    assert rows[0]["stars"] == 43
    history = get_history(conn, "some-org/some-repo")
    assert len(history) == 2
    # newest first
    assert history[0]["stars"] == 43
    assert history[1]["stars"] == 42


def test_get_repos_orders_by_stars(tmp_path):
    conn = connect(_tmp_db(tmp_path))
    low = normalize_repo({**RAW_REPO, "full_name": "o/low", "name": "low",
                          "stargazers_count": 1})
    high = normalize_repo({**RAW_REPO, "full_name": "o/high", "name": "high",
                           "stargazers_count": 99})
    upsert_repos(conn, [low, high])
    rows = get_repos(conn)
    assert [r["full_name"] for r in rows] == ["o/high", "o/low"]


def test_history_limit(tmp_path):
    conn = connect(_tmp_db(tmp_path))
    upsert_repos(conn, [normalize_repo(RAW_REPO)], synced_at="2026-09-15T00:00:00Z")
    upsert_repos(conn, [normalize_repo(RAW_REPO)], synced_at="2026-09-16T00:00:00Z")
    upsert_repos(conn, [normalize_repo(RAW_REPO)], synced_at="2026-09-17T00:00:00Z")
    assert len(get_history(conn, "some-org/some-repo", limit=2)) == 2
