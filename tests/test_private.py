"""Tests for the ``sync --private`` flag (include_private on list_repos).

Covers the client-side URL switch (type=all vs type=public) and the CLI's
fail-fast when no GH_TOKEN is present, all offline via monkeypatch so the
test suite never talks to the real API.
"""

import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import reporadar.client as client  # noqa: E402
from reporadar import main as cli  # noqa: E402


class _FakeResp:
    """Minimal stand-in for urlopen's context manager result."""

    def __init__(self, body: list) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self._body).encode()


def _capture_urls(monkeypatch, repos: list):
    """Patch urlopen to record request URLs and return ``repos``."""
    urls: list[str] = []

    def fake_urlopen(req):
        urls.append(req.full_url)
        return _FakeResp(repos)

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
    return urls


def test_list_repos_public_by_default(monkeypatch):
    """Without include_private the query uses type=public."""
    urls = _capture_urls(monkeypatch, [])
    client.list_repos("org")
    assert len(urls) == 1
    assert "type=public" in urls[0]
    assert "type=all" not in urls[0]


def test_list_repos_include_private_switches_to_all(monkeypatch):
    """With include_private=True the query uses type=all."""
    urls = _capture_urls(monkeypatch, [])
    client.list_repos("org", token="t", include_private=True)
    assert len(urls) == 1
    assert "type=all" in urls[0]
    assert "type=public" not in urls[0]


def test_list_repos_private_still_paginates(monkeypatch):
    """type=all must follow the same Link-based pagination as public."""
    pages = {"page1": ["https://api.github.com/users/org/repos?per_page=100&type=all&page=2"]}
    calls = []

    class _Resp:
        def __init__(self, body, next_url=None):
            self._body = body
            self.headers = {}
            if next_url:
                self.headers["Link"] = f'<{next_url}>; rel="next"'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(self._body).encode()

    def fake_urlopen(req):
        calls.append(req.full_url)
        if "page=2" in req.full_url:
            return _Resp([{"full_name": "org/b"}])
        return _Resp([{"full_name": "org/a"}], next_url="https://api.github.com/users/org/repos?per_page=100&type=all&page=2")

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
    repos = client.list_repos("org", token="t", include_private=True)
    assert [r["full_name"] for r in repos] == ["org/a", "org/b"]
    # every request kept type=all (pagination preserved the param)
    assert all("type=all" in u for u in calls)


RAW_PRIV = {
    "full_name": "org/secret",
    "name": "secret",
    "owner": {"login": "org"},
    "language": "Go",
    "stargazers_count": 3,
    "forks_count": 0,
    "open_issues_count": 1,
    "archived": False,
    "created_at": "2025-07-01T00:00:00Z",
}


def test_sync_private_includes_private_repos_in_db(tmp_path, monkeypatch, capsys):
    """sync --private stores the private repo that a plain sync would miss."""
    db = str(tmp_path / "r.db")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GH_TOKEN", "test-token")

    seen_include_private: list[bool] = []

    def fake_list(owner, token=None, include_private=False):
        seen_include_private.append(include_private)
        # a private repo appears only when the caller asked for all
        return [RAW_PRIV] if include_private else [
            {**RAW_PRIV, "full_name": "org/public", "name": "public"}
        ]

    monkeypatch.setattr("reporadar.main.list_repos", fake_list)
    rc = cli.sync_many(["org"], db, private=True)
    assert rc == 0
    # the flag actually propagated through to the client call
    assert seen_include_private == [True]
    capsys.readouterr()

    conn = cli._open_db(db)
    try:
        rows = cli.get_repos(conn)
    finally:
        conn.close()
    names = {r["full_name"] for r in rows}
    assert "org/secret" in names


def test_sync_private_without_token_fails_fast(tmp_path, monkeypatch, capsys):
    """No GH_TOKEN + --private -> exit 1 with a clear message, no fetch."""
    db = str(tmp_path / "r.db")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    called = []
    monkeypatch.setattr(
        "reporadar.main.list_repos",
        lambda owner, token=None, include_private=False: called.append(owner) or [],
    )
    rc = cli.sync_many(["org"], db, private=True)
    assert rc == 1
    err = capsys.readouterr().err
    assert "GH_TOKEN" in err and "--private" in err
    # fail-fast means we never even attempted a fetch
    assert called == []


def test_sync_without_private_flag_stays_public(tmp_path, monkeypatch, capsys):
    """A plain sync must not flip to type=all (no accidental private leak)."""
    db = str(tmp_path / "r.db")
    monkeypatch.chdir(tmp_path)

    seen_include_private: list[bool] = []
    monkeypatch.setattr(
        "reporadar.main.list_repos",
        lambda owner, token=None, include_private=False: (
            seen_include_private.append(include_private), [])[1],
    )
    rc = cli.sync_many(["org"], db)
    assert rc == 0
    assert seen_include_private == [False]


def test_cli_parser_has_private_flag():
    """The sync subcommand exposes --private (argparse wiring)."""
    parser = cli.build_parser()
    args = parser.parse_args(["sync", "org", "--db", "x.db", "--private"])
    assert args.private is True
    # and it's off by default
    args2 = parser.parse_args(["sync", "org"])
    assert args2.private is False
