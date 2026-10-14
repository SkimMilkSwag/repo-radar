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
    history REPO [--limit N] [--since DAYS]
                                Print stored snapshots for one repo, newest
                                first, with star/fork deltas vs the previous
                                snapshot; --since windows it to the last N
                                days.
    report [--db PATH]          Per-language repo count + star/fork totals as
                                a markdown table, with growth vs the oldest
                                snapshot in the db.
    export [--csv PATH]         Dump the current-state table: JSON on stdout,
                                or CSV via ``--csv`` (file path, or ``-`` for
                                stdout).  Exits 1 when the db is empty.
    search TERM [--limit N]     Full-text search over repo names and
                                descriptions (SQLite FTS5; LIKE fallback on
                                minimal builds).  Multi-word terms match any
                                word; results are most-starred first.

``--json`` (top and history) prints the same data as a JSON document
instead of a table: for ``top`` a list of repo objects; for ``history``
a list of snapshot objects, each with ``delta_stars`` / ``delta_forks``
vs the previous snapshot (null on the oldest one).  Both accept
``--db PATH`` as usual.

Exit codes: 0 on success, 1 on API/storage errors or an empty db during
export, 2 on usage errors (argparse's default).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from . import __version__
from .client import APIError, list_repos
from .normalize import normalize_repo, summarize
from .storage import connect, get_history, get_repos, search_repos, upsert_repos

DEFAULT_DB = "repo_radar.db"

# Columns exported for the current-state table (repos).  description is
# deliberately last: it's the only free-text field and can contain commas,
# so CSV consumers that naively split on commas lose nothing from the
# fixed fields.
EXPORT_CSV_COLUMNS = [
    "full_name",
    "owner",
    "name",
    "language",
    "stars",
    "forks",
    "open_issues",
    "archived",
    "created_at",
    "last_synced_at",
    "description",
]


def _open_db(path: str) -> sqlite3.Connection:
    return connect(path)


def cmd_sync(args: argparse.Namespace) -> int:
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


def cmd_top(args: argparse.Namespace) -> int:
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

    def fmt(row: dict[str, Any]) -> str:
        lang = (row["language"] or "-")[:12]
        return f"{row['stars']:<6} {row['forks']:<5} {lang:<13} {row['full_name']}"

    header = f"{'STARS':<6} {'FORKS':<5} {'LANG':<13} REPO"
    print(header)
    print("-" * len(header))
    for row in rows:
        print(fmt(row))
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    """Print (or emit as JSON) a repo's snapshots newest-first with deltas."""
    since = None
    if args.since is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=args.since)
        since = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = _open_db(args.db)
    try:
        history = get_history(conn, args.repo, limit=args.limit, since=since)
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
        window = f" (since {args.since}d ago)" if args.since is not None else ""
        print(f"no history for {args.repo!r} in {args.db}{window}")
        return 0

    for snap, d_stars, d_forks in entries:
        s_delta = "" if d_stars is None else _delta(d_stars)
        f_delta = "" if d_forks is None else _delta(d_forks)
        print(
            f"{snap['synced_at']}  stars {snap['stars']:<6}{s_delta:>7}  "
            f"forks {snap['forks']:<5}{f_delta:>7}"
        )
    return 0


def cmd_report(args: argparse.Namespace) -> int:
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


def _row_to_csv_value(value: Any) -> Any:
    """Map a db cell to its export representation (shared by JSON + CSV).

    NULL becomes the empty string (a literal ``None`` would render as the
    text ``None`` in CSV, which looks like real data), and booleans — or
    SQLite's 0/1 integer stand-ins for them — become lowercase
    ``true``/``false`` so both formats agree.
    """
    if value is None:
        return ""
    if isinstance(value, bool) or value in (0, 1):
        return "true" if value else "false"
    return value


def cmd_export(args: argparse.Namespace) -> int:
    """Dump the current snapshot as JSON (default) or CSV (--csv).

    Both formats carry the same rows and column order, most-starred repo
    first.  Without ``--csv``, stdout is a JSON list of repo objects;
    with it, the CSV goes to the named file (``-`` selects stdout) and
    stdout stays clean for piping::

        repo-radar export                  # JSON on stdout
        repo-radar export --csv snap.csv   # CSV file + stderr note

    NULL cells render as "" in both formats; booleans as true/false in
    CSV and real booleans in JSON.  Exit codes: 0 on success, 1 when the
    db is empty or the CSV write fails.
    """
    conn = _open_db(args.db)
    try:
        rows = get_repos(conn)
    finally:
        conn.close()
    if not rows:
        print(f"no repos in {args.db} — run 'repo-radar sync <owner>' first")
        return 1

    if args.csv is not None:
        try:
            stream = (sys.stdout if args.csv == "-" else open(args.csv, "w",
                                                              newline=""))
        except OSError as exc:
            print(f"error: writing {args.csv}: {exc}", file=sys.stderr)
            return 1
        try:
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(EXPORT_CSV_COLUMNS)
            for row in rows:
                writer.writerow(
                    _row_to_csv_value(row.get(c)) for c in EXPORT_CSV_COLUMNS
                )
            # Buffer first, then flush: a write error (e.g. bad path)
            # must not leave a half-written CSV behind while the JSON
            # still went to stdout.
            payload = buf.getvalue()
            stream.write(payload)
        except OSError as exc:
            print(f"error: writing {args.csv}: {exc}", file=sys.stderr)
            return 1
        finally:
            if args.csv != "-":
                stream.close()
    else:
        # No --csv: stdout is the machine-readable payload, same columns
        # and row order as the CSV.
        json_payload = [
            {c: _row_to_csv_value(row.get(c)) for c in EXPORT_CSV_COLUMNS}
            for row in rows
        ]
        print(json.dumps(json_payload, indent=2))

    if args.csv is not None and args.csv != "-":
        print(f"exported {len(rows)} repo(s) to {args.csv}", file=sys.stderr)
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    """Full-text search over tracked repos (names + descriptions).

    Multi-word terms are OR'd word-by-word, so ``search deploy ci`` finds
    repos matching either word.  Output is a table in the same shape as
    ``top`` — stars, forks, language, repo name — with a short description
    line under each match; ``--json`` emits the full row objects instead.
    Exit codes: 0 on success (including no matches), 1 when the db is empty.
    """
    conn = _open_db(args.db)
    try:
        rows = search_repos(conn, args.term, limit=args.limit)
        empty = not get_repos(conn)
    finally:
        conn.close()

    if args.json:
        print(json.dumps([dict(r) for r in rows], indent=2))
        return 0 if rows or not empty else 1
    if empty:
        print(f"no repos in {args.db} — run 'repo-radar sync <owner>' first")
        return 1
    if not rows:
        print(f"no repo matches {args.term!r} in {args.db}")
        return 0

    header = f"{'STARS':<6} {'FORKS':<5} {'LANG':<13} REPO"
    print(header)
    print("-" * len(header))
    for row in rows:
        lang = (row["language"] or "-")[:12]
        print(f"{row['stars']:<6} {row['forks']:<5} {lang:<13} {row['full_name']}")
        if row.get("description"):
            print(f"       {row['description']}")
    return 0


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
    p_hist.add_argument("--since", type=int, metavar="DAYS", default=None,
                        help="only show snapshots from the last DAYS days")
    p_hist.add_argument("--db", default=DEFAULT_DB)
    p_hist.add_argument("--json", action="store_true",
                        help="print a JSON list of snapshots (with deltas)")
    p_hist.set_defaults(func=cmd_history)

    p_report = sub.add_parser("report", help="per-language totals + growth")
    p_report.add_argument("--db", default=DEFAULT_DB,
                          help=f"SQLite db path (default {DEFAULT_DB})")
    p_report.set_defaults(func=cmd_report)

    p_export = sub.add_parser(
        "export", help="dump the current snapshot as CSV and/or JSON")
    p_export.add_argument("--csv", metavar="PATH", default=None,
                          help="write a CSV file to PATH ('-' for stdout); "
                               "JSON is always printed on stdout")
    p_export.add_argument("--db", default=DEFAULT_DB,
                          help=f"SQLite db path (default {DEFAULT_DB})")
    p_export.set_defaults(func=cmd_export)

    p_search = sub.add_parser(
        "search", help="full-text search over repo names and descriptions")
    p_search.add_argument("term",
                          help="search term; multi-word terms match any word")
    p_search.add_argument("--limit", type=int, default=20,
                          help="max matches to show (default 20)")
    p_search.add_argument("--db", default=DEFAULT_DB)
    p_search.add_argument("--json", action="store_true",
                          help="print a JSON list of matching repo rows")
    p_search.set_defaults(func=cmd_search)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    func = args.func
    assert callable(func), "subparser missing its handler"
    return int(func(args))


if __name__ == "__main__":
    sys.exit(main())
