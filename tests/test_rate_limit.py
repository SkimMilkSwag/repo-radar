"""Tests for client.py's rate-limit backoff path (403 + remaining=0).

These isolate the one branch of fetch_json that sleeps and retries, so a
regression there is caught without hitting the real API.
"""

import json
import os
import sys
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import reporadar.client as client  # noqa: E402


class _FakeResp:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _rate_limited_error():
    return urllib.error.HTTPError(
        "url", 403, "rate limited",
        {"X-RateLimit-Remaining": "0"},
        __import__("io").BytesIO(b'{"message":"rate limit exceeded"}'),
    )


def test_fetch_json_retries_once_after_rate_limit(monkeypatch):
    """First call 403+remaining=0 -> sleep + retry; second call succeeds."""
    calls = []

    def fake_urlopen(req):
        calls.append(1)
        if len(calls) == 1:
            raise _rate_limited_error()
        return _FakeResp(json.dumps({"recovered": True}).encode())

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
    slept = []
    monkeypatch.setattr(client.time, "sleep", lambda s: slept.append(s))

    result = client.fetch_json("https://example.invalid/x")
    assert result == {"recovered": True}
    assert len(calls) == 2  # exactly one retry
    assert slept == [client.RATE_LIMIT_SLEEP]


def test_fetch_json_raises_after_failed_retry(monkeypatch):
    """If the retry is also rate-limited, give up with an APIError."""
    calls = []

    def fake_urlopen(req):
        calls.append(1)
        raise _rate_limited_error()

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(client.time, "sleep", lambda s: None)

    try:
        client.fetch_json("https://example.invalid/x")
        assert False, "expected APIError"
    except client.APIError as e:
        assert e.status == 403
    assert len(calls) == 2  # original + one retry, then give up


def test_fetch_json_no_retry_on_plain_404(monkeypatch):
    """A non-rate-limit error (e.g. 404) must not trigger the backoff path."""
    calls = []

    def fake_urlopen(req):
        calls.append(1)
        raise urllib.error.HTTPError(
            "url", 404, "nope", None, __import__("io").BytesIO(b"{}"))

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
    slept = []
    monkeypatch.setattr(client.time, "sleep", lambda s: slept.append(s))

    try:
        client.fetch_json("https://example.invalid/x")
        assert False, "expected APIError"
    except client.APIError as e:
        assert e.status == 404
    assert len(calls) == 1  # no retry
    assert slept == []
