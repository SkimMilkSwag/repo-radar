"""repo-radar CLI.

Subcommands:

    sync OWNER... [--db PATH]   Fetch the owners' public repos from GitHub
                                and upsert them into the local db (default
                                ./repo_radar.db).  One or more owners can be
                                synced in a single run; per-owner errors are
                                reported without aborting the rest.  Prints
                                a one-line summary per owner.
    top [N] [--db PATH]         Show the N most-starred tracked repos
                                (default 10) as a small table.
    history REPO [--limit N]    Print stored snapshots for one repo, newest
                                first, with star/fork deltas vs the previous
                                snapshot.
    report [--db PATH]          Per-language repo count + star/fork totals as
                                a markdown table, with growth vs the oldest
                                snapshot in the db.

``--json`` (top and history) prints the same data as a JSON document
instead of a table: for ``top`` a list of repo objects; for ``history``
a list of snapshot objects, each with ``delta_stars`` / ``delta_forks``
vs the previous snapshot (null on the oldest one).  Both accept
``--db PATH`` as usual.

Exit codes: 0 on success, 1 on API/storage errors, 2 on usage errors
(argparse's default).
"""

from __future__ import annotations

import argparse
import json
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
    """Fetch one or more owners' repos and upsert them into the db."""
    return sync_many(args.owners, args.db)


def sync_many(owners: list[str], db_path: str) -> int:
    """Fetch several owners' repos in one run and merge them into ``db_path``.

    Each owner's repos are normalized and upserted under the same
    ``full_name`` key, so repos shared by multiple owners (forks) converge
    to the latest value; per-owner errors are reported and counted instead
    of aborting the whole run.  Exit status mirrors a single-owner sync:
    0 when every owner succeeded, 1 when at least one failed.
    """
    token = os.environ.get("GH_TOKEN")
    failures = 0
    for owner in owners:
        try:
            raw = list_repos(owner, token=token)
        except APIError as exc:
            print(f"error: {owner}: {exc}", file=sys.stderr)
            failures += 1
            continue

        repos = [normalize_repo(r) for r in raw]
        conn = _open_db(db_path)
        try:
            upsert_repos(conn, repos)
        finally:
            conn.close()

        s = summarize(repos)
        langs = ", ".join(f"{k}:{v}" for k, v in list(s["languages"].items())[:5])
        print(
            f"synced {len(repos)} repo(s) for {owner}: "
            f"{s['total_stars']} stars, {s['total_forks']} forks ({langs})"
        )
    return 1 if failures else 0


def cmd_top(args) -> int:
    """Print (or emit as JSON) the most-starred tracked repos."""
    conn = _open_db(args.db)
    try:
        rows = get_repos(conn)[: args.n]
    finally:
        conn.close()
    if args.json:
        print(json.dumps([dict(r) for r in rows], indent=2))
        return 0
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
    """Print (or emit as JSON) a repo's snapshots newest-first with deltas."""
    conn = _open_db(args.db)
    try:
        history = get_history(conn, args.repo, limit=args.limit)
    finally:
        conn.close()

    # get_history returns newest first; deltas need the older row next.
    entries = []
    for i, snap in enumerate(history):
        prev = history[i + 1] if i + 1 < len(history) else None
        d_stars = None if prev is None else snap["stars"] - prev["stars"]
        d_forks = None if prev is None else snap["forks"] - prev["forks"]
        entries.append((snap, d_stars, d_forks))

    if args.json:
        payload = [
            {**dict(snap), "delta_stars": ds, "delta_forks": df}
            for snap, ds, df in entries
        ]
        print(json.dumps(payload, indent=2))
        return 0
    if not entries:
        print(f"no history for {args.repo!r} in {args.db}")
        return 0

    for snap, d_stars, d_forks in entries:
        s_delta = "" if d_stars is None else _delta(d_stars)
        f_delta = "" if d_forks is None else _delta(d_forks)
        print(
            f"{snap['synced_at']}  stars {snap['stars']:<6}{s_delta:>7}  "
            f"forks {snap['forks']:<5}{f_delta:>7}"
        )
    return 0


def cmd_report(args) -> int:
    """Print per-language totals as a markdown table.

    Language rows show the current repo count and star/fork sums from the
    latest state, plus deltas vs the oldest snapshot in this db (i.e.
    growth since tracking started).  The ``Total`` row covers every
    tracked repo; when only one snapshot exists, nothing has moved yet,
    so all deltas are 0.
    """
    conn = _open_db(args.db)
    try:
        repos = get_repos(conn)
        history = get_history(conn, full_name=None)
    finally:
        conn.close()
    if not repos:
        print(f"no repos in {args.db} — run 'repo-radar sync <owner>' first")
        return 0

    lang_stats: dict[str, dict[str, int]] = {}
    for row in repos:
        stats = lang_stats.setdefault(row["language"] or "(none)",
                                      {"repos": 0, "stars": 0, "forks": 0})
        stats["repos"] += 1
        stats["stars"] += row["stars"]
        stats["forks"] += row["forks"]

    # Baseline = the oldest snapshot in the db; deltas measure everything
    # since tracking started.  The very first sync has no baseline and
    # reports 0 across the board.
    oldest_time = min(h["synced_at"] for h in history)
    baseline: dict[str, list[int]] = {}
    for snap in history:
        if snap["synced_at"] == oldest_time:
            b = baseline.setdefault(snap["language"] or "(none)", [0, 0])
            b[0] += snap["stars"]
            b[1] += snap["forks"]

    def delta_field(lang: str, idx: int) -> str:
        """+N / -N / 0 vs the baseline; '—' for a language added later."""
        if lang not in baseline:
            return "—"
        current = sum(
            r["stars" if idx == 0 else "forks"] for r in repos
            if (r["language"] or "(none)") == lang
        )
        return _delta(current - baseline[lang][idx])

    print("# repo-radar report")
    print()
    print("| LANG | REPOS | STARS | Δ | FORKS | Δ |")
    print("| --- | ---: | ---: | ---: | ---: | ---: |")
    total = {"repos": 0, "stars": 0, "forks": 0}
    for lang in sorted(lang_stats, key=lambda l: (-lang_stats[l]["stars"], l)):
        s = lang_stats[lang]
        for k in total:
            total[k] += s[k]
        print(f"| {lang} | {s['repos']} | {s['stars']} "
              f"| {delta_field(lang, 0)} | {s['forks']} "
              f"| {delta_field(lang, 1)} |")
    base_stars = sum(v[0] for v in baseline.values()) or None
    base_forks = sum(v[1] for v in baseline.values()) or None
    print(f"| **Total** | {total['repos']} | {total['stars']} "
          f"| {_fmt_delta(total['stars'], base_stars)} | {total['forks']} "
          f"| {_fmt_delta(total['forks'], base_forks)} |")
    return 0


def _delta(value: int) -> str:
    """Format a change as +N / -N / 0 for tables."""
    if value > 0:
        return f"+{value}"
    return str(value)


def _fmt_delta(current: int, baseline: int | None) -> str:
    """Delta vs a baseline; renders ``—`` when there is no baseline yet."""
    if baseline is None:
        return "—"
    return _delta(current - baseline)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="repo-radar",
        description="Snapshot an org's GitHub repos into a local SQLite db.",
    )
    parser.add_argument("--version", action="version",
                        version=f"repo-radar {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync", help="fetch one or more owners into the db")
    p_sync.add_argument("owners", nargs="+", metavar="OWNER",
                        help="GitHub org or user name; repeat to sync several")
    p_sync.add_argument("--db", default=DEFAULT_DB,
                        help=f"SQLite db path (default {DEFAULT_DB})")
    p_sync.set_defaults(func=cmd_sync)

    p_top = sub.add_parser("top", help="show the most-starred tracked repos")
    p_top.add_argument("n", nargs="?", type=int, default=10,
                       help="how many rows to print (default 10)")
    p_top.add_argument("--db", default=DEFAULT_DB)
    p_top.add_argument("--json", action="store_true",
                       help="print a JSON list of repos instead of a table")
    p_top.set_defaults(func=cmd_top)

    p_hist = sub.add_parser("history", help="show a repo's snapshot history")
    p_hist.add_argument("repo", help="repo full name, e.g. owner/name")
    p_hist.add_argument("--limit", type=int, default=20,
                        help="max snapshots to show (default 20)")
    p_hist.add_argument("--db", default=DEFAULT_DB)
    p_hist.add_argument("--json", action="store_true",
                        help="print a JSON list of snapshots (with deltas)")
    p_hist.set_defaults(func=cmd_history)

    p_report = sub.add_parser("report", help="per-language totals + growth")
    p_report.add_argument("--db", default=DEFAULT_DB,
                          help=f"SQLite db path (default {DEFAULT_DB})")
    p_report.set_defaults(func=cmd_report)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
