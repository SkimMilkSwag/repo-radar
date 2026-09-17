"""repo-radar CLI.

Subcommands:

    sync OWNER [--db PATH]      Fetch OWNER's public repos from GitHub and
                                upsert them into the local db (default
                                ./repo_radar.db). Prints a one-line summary.
    top [N] [--db PATH]         Show the N most-starred tracked repos
                                (default 10) as a small table.
    history REPO [--limit N]    Print stored snapshots for one repo, newest
                                first, with star/fork deltas vs the previous
                                snapshot.

Exit codes: 0 on success, 1 on API/storage errors, 2 on usage errors
(argparse's default).
"""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .client import APIError, list_repos
from .normalize import normalize_repo, summarize
from .storage import connect, get_history, get_repos, upsert_repos

DEFAULT_DB = "repo_radar.db"


def _open_db(path: str):
    return connect(path)


def cmd_sync(args) -> int:
    """Fetch an owner's repos and upsert them into the db."""
    token = os.environ.get("GH_TOKEN")
    try:
        raw = list_repos(args.owner, token=token)
    except APIError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    repos = [normalize_repo(r) for r in raw]
    conn = _open_db(args.db)
    try:
        written = upsert_repos(conn, repos)
    finally:
        conn.close()

    s = summarize(repos)
    langs = ", ".join(f"{k}:{v}" for k, v in list(s["languages"].items())[:5])
    print(
        f"synced {written} repo(s) for {args.owner}: "
        f"{s['total_stars']} stars, {s['total_forks']} forks ({langs})"
    )
    return 0


def cmd_top(args) -> int:
    """Print the most-starred tracked repos as a fixed-width table."""
    conn = _open_db(args.db)
    try:
        rows = get_repos(conn)[: args.n]
    finally:
        conn.close()
    if not rows:
        print(f"no repos in {args.db} — run 'repo-radar sync <owner>' first")
        return 0

    def fmt(row) -> str:
        lang = (row["language"] or "-")[:12]
        return f"{row['stars']:<6} {row['forks']:<5} {lang:<13} {row['full_name']}"

    header = f"{'STARS':<6} {'FORKS':<5} {'LANG':<13} REPO"
    print(header)
    print("-" * len(header))
    for row in rows:
        print(fmt(row))
    return 0


def cmd_history(args) -> int:
    """Print a repo's snapshots newest-first with deltas vs the previous one."""
    conn = _open_db(args.db)
    try:
        history = get_history(conn, args.repo, limit=args.limit)
    finally:
        conn.close()
    if not history:
        print(f"no history for {args.repo!r} in {args.db}")
        return 0

    # get_history returns newest first; deltas need the older row next.
    for i, snap in enumerate(history):
        prev = history[i + 1] if i + 1 < len(history) else None
        d_stars = d_forks = ""
        if prev is not None:
            d_stars = _delta(snap["stars"] - prev["stars"])
            d_forks = _delta(snap["forks"] - prev["forks"])
        print(
            f"{snap['synced_at']}  stars {snap['stars']:<6}{d_stars:>7}  "
            f"forks {snap['forks']:<5}{d_forks:>7}"
        )
    return 0


def _delta(value: int) -> str:
    """Format a change as +N / -N / 0 for the history table."""
    if value > 0:
        return f"+{value}"
    return str(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="repo-radar",
        description="Snapshot an org's GitHub repos into a local SQLite db.",
    )
    parser.add_argument("--version", action="version",
                        version=f"repo-radar {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync", help="fetch an owner's repos into the db")
    p_sync.add_argument("owner", help="GitHub org or user name")
    p_sync.add_argument("--db", default=DEFAULT_DB,
                        help=f"SQLite db path (default {DEFAULT_DB})")
    p_sync.set_defaults(func=cmd_sync)

    p_top = sub.add_parser("top", help="show the most-starred tracked repos")
    p_top.add_argument("n", nargs="?", type=int, default=10,
                       help="how many rows to print (default 10)")
    p_top.add_argument("--db", default=DEFAULT_DB)
    p_top.set_defaults(func=cmd_top)

    p_hist = sub.add_parser("history", help="show a repo's snapshot history")
    p_hist.add_argument("repo", help="repo full name, e.g. owner/name")
    p_hist.add_argument("--limit", type=int, default=20,
                        help="max snapshots to show (default 20)")
    p_hist.add_argument("--db", default=DEFAULT_DB)
    p_hist.set_defaults(func=cmd_history)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
