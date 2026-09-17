"""Thin client for the GitHub REST API (v3), stdlib-only.

Wraps ``urllib`` so the rest of the package never touches sockets directly —
tests monkeypatch :func:`fetch_json`.  Handles:

* optional auth token (``GH_TOKEN`` env var or explicit argument)
* pagination via ``Link: rel="next"`` headers
* rate-limit backoff: a single retry after sleeping when we hit the
  ``X-RateLimit-Remaining: 0`` response
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

# Seconds to wait before retrying a rate-limited (403, remaining=0) request.
# Kept small: the CLI is a human-paced tool, not a polling service.
RATE_LIMIT_SLEEP = 5


class APIError(Exception):
    """Raised when GitHub returns a non-retryable HTTP error."""

    def __init__(self, status: int, detail: str = "") -> None:
        super().__init__(f"GitHub API error {status}: {detail}")
        self.status = status
        self.detail = detail


def fetch_json(url: str, token: str | None = None) -> dict | list:
    """GET ``url`` and decode the JSON body.

    Retries once after ``RATE_LIMIT_SLEEP`` seconds when the response
    indicates the hourly rate limit is exhausted (HTTP 403 with
    ``X-RateLimit-Remaining: 0``).
    """
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "repo-radar"}
    if token:
        headers["Authorization"] = f"token {token}"

    last_exc: urllib.error.HTTPError | None = None
    for attempt in range(2):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:  # 403/404 etc.
            last_exc = exc
            remaining = (exc.headers or {}).get("X-RateLimit-Remaining")
            if attempt == 0 and exc.code == 403 and remaining == "0":
                time.sleep(RATE_LIMIT_SLEEP)
                continue
            body = ""
            try:
                body = exc.read().decode()[:200]
            except Exception:
                pass
            raise APIError(exc.code, body) from exc
    # unreachable: loop either returned or raised
    raise APIError(last_exc.code if last_exc else 0, "rate-limit retry failed")


def next_url(headers: dict | None) -> str | None:
    """Extract the ``rel="next"`` URL from a ``Link`` header, if present."""
    if not headers:
        return None
    link = headers.get("Link", "")
    for part in link.split(","):
        segments = [s.strip() for s in part.split(";")]
        if len(segments) >= 2 and 'rel="next"' in segments[1]:
            return segments[0].strip("<>")
    return None


def list_repos(owner: str, token: str | None = None) -> list[dict]:
    """Return every public repo of ``owner`` (follows pagination).

    Pages through ``/orgs/{owner}/repos`` or ``/users/{owner}/repos`` —
    both accept ``per_page=100``.  GitHub caps a single page at 100, so an
    org with >100 repos is fetched across multiple requests.
    """
    if token is None:
        token = os.environ.get("GH_TOKEN")
    url = f"https://api.github.com/users/{owner}/repos?per_page=100&type=public"
    repos: list[dict] = []
    while url:
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": "repo-radar",
                     **({"Authorization": f"token {token}"} if token else {})},
        )
        with urllib.request.urlopen(req) as resp:
            repos.extend(json.load(resp))
            url = next_url(dict(resp.headers))
    return repos
