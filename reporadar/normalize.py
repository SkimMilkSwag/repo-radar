"""Normalize raw GitHub repo payloads into flat, DB-ready rows.

GitHub's API responses are verbose and slightly inconsistent across
endpoints (``null`` vs missing fields, nested ``owner`` objects).  The
functions here reduce them to the columns repo-radar actually tracks, so
storage and reporting never deal with raw JSON.
"""

from __future__ import annotations

# Columns tracked in both the current-state and history tables.
REPO_COLUMNS = (
    "full_name",
    "name",
    "owner",
    "description",
    "language",
    "stars",
    "forks",
    "open_issues",
    "archived",
    "created_at",
)


# GitHub payloads are JSON: loose dicts.  ``Any`` is intentional here —
# the normalizer is where untyped data becomes typed rows.
from typing import Any


def normalize_repo(payload: dict[str, Any]) -> dict[str, Any]:
    """Map one raw repo payload to a flat row keyed by ``REPO_COLUMNS``.

    Missing / null fields become safe defaults: counts default to 0,
    the language and description to ``None`` (SQLite NULL), and archived
    to False.  Nested ``owner.login`` is flattened to ``owner``.
    """
    owner_raw = payload.get("owner")
    if not isinstance(owner_raw, dict):
        owner_raw = {}
    return {
        "full_name": payload.get("full_name", ""),
        "name": payload.get("name", ""),
        "owner": owner_raw.get("login"),
        "description": payload.get("description"),
        "language": payload.get("language"),
        "stars": int(payload.get("stargazers_count") or 0),
        "forks": int(payload.get("forks_count") or 0),
        "open_issues": int(payload.get("open_issues_count") or 0),
        "archived": bool(payload.get("archived", False)),
        "created_at": payload.get("created_at"),
    }


def summarize(repos: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate normalized rows into a snapshot summary.

    Returns repo count, total stars/forks, and a language breakdown
    (most-used first).  Repos without a detected language are bucketed
    as ``"None"`` so the totals always add up to ``repo_count``.
    """
    by_language: dict[str, int] = {}
    for repo in repos:
        lang = repo.get("language") or "None"
        by_language[lang] = by_language.get(lang, 0) + 1

    return {
        "repo_count": len(repos),
        "total_stars": sum(r.get("stars", 0) for r in repos),
        "total_forks": sum(r.get("forks", 0) for r in repos),
        "languages": dict(sorted(by_language.items(),
                                 key=lambda kv: (-kv[1], kv[0]))),
    }
