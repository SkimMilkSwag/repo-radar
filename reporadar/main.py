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
    report [--db PATH] [--since DAYS]
                                Per-language repo count + star/fork totals as
                                a markdown table, with growth vs the oldest
                                snapshot in the db (or within the last DAYS
                                days when --since is given).
    export [--csv PATH]         Dump the current-state table: JSON on stdout,
                                or CSV via ``--csv`` (file path, or ``-`` for
                                stdout).  Exits 1 when the db is empty.
    search TERM [--limit N]     Full-text search over repo names and
                                descriptions (SQLite FTS5; LIKE fallback on
                                minimal builds).  Multi-word terms match any
                                word; results are most-starred first.
    watch OWNER... [--db PATH]  Sync the owners' repos and print only the
                                repos whose stars or forks changed since the
                                previous snapshot in this db.  New repos
                                (first seen) are listed as "new".  A quiet
                                run (no changes) prints a single line and
                                exits 0 — handy for cron jobs.
    top-gainers [N] [--since DAYS] [--db PATH]
                                Rank tracked repos by stars gained since the
                                first snapshot of each repo (or since the
                                last DAYS days when --since is given).
                                New repos added after tracking started show
                                their current star count as the gain.
    diff OLD NEW [--db PATH]    Compare two named snapshots: repos added
                                after OLD, removed before or at NEW, and the
                                star/fork movement of surviving repos between
                                the two timestamps.  Names are resolved to
                                stored snapshot times via ``snapshots``.
    prune OWNER... [--dry-run]  Re-fetch the owners' repos and drop tracked
                                repos that are gone from GitHub (deleted or
                                renamed, i.e. no longer in a successful
                                re-fetch).  History rows are kept; --dry-run
                                lists what would be removed without deleting.

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
from typing import Any, TypedDict


class _DiffRepoEntry(TypedDict):
    full_name: str
    stars: int
    forks: int


class _DiffMovedEntry(_DiffRepoEntry):
    old_stars: int
    delta_stars: int
    old_forks: int
    delta_forks: int


class _DiffPayload(TypedDict):
    old: str
    new: str
    added: list[_DiffRepoEntry]
    removed: list[_DiffRepoEntry]
    moved: list[_DiffMovedEntry]

from . import __version__
from .client import APIError, list_repos
from .normalize import normalize_repo, summarize
from .storage import (connect, get_first_snapshots, get_gone_repos,
                      get_history, get_latest_snapshots, get_repos,
                      list_snapshot_times, resolve_snapshot_time,
                      remove_repos, search_repos, snapshots_at_or_before,
                      snapshots_between, upsert_repos)

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
    return sync_many(args.owners, args.db, private=args.private)


def sync_many(
    owners: list[str], db_path: str, private: bool = False
) -> int:
    """Fetch several owners' repos in one run and merge them into ``db_path``.

    Each owner's repos are normalized and upserted under the same
    ``full_name`` key, so repos shared by multiple owners (forks) converge
    to the latest value; per-owner errors are reported and counted instead
    of aborting the whole run.  Exit status mirrors a single-owner sync:
    0 when every owner succeeded, 1 when at least one failed.

    With ``private=True`` the underlying ``list_repos`` call passes
    ``type=all`` so private repos are included; that requires ``GH_TOKEN``
    to carry ``repo`` scope (the API returns 404 for a token without it).
    """
    token = os.environ.get("GH_TOKEN")
    if private and not token:
        # no token at all can't see any private repo — fail fast with a
        # specific message rather than a confusing 404 per owner.
        print(
            "error: --private requires GH_TOKEN to be set (with `repo` scope)",
            file=sys.stderr,
        )
        return 1
    failures = 0
    for owner in owners:
        try:
            raw = list_repos(owner, token=token, include_private=private)
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
    growth since tracking started).  With ``--since DAYS`` the baseline is
    instead the oldest snapshot within that window, so the table reports
    growth over the recent period rather than all time.  The ``Total`` row
    covers every tracked repo; when only one snapshot exists (or none in
    the window), nothing has moved yet, so all deltas are 0 / ``—``.
    """
    since = None
    if args.since is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=args.since)
        since = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = _open_db(args.db)
    try:
        repos = get_repos(conn)
        history = get_history(conn, full_name=None, since=since)
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

    # Baseline = the oldest snapshot in scope (all history, or the
    # --since window); deltas measure growth since then.  A single
    # snapshot — or an empty window — has no baseline and reports
    # 0 / '—' across the board.
    if history:
        oldest_time = min(h["synced_at"] for h in history)
    else:
        oldest_time = None
    baseline: dict[str, list[int]] = {}
    for snap in history:
        if oldest_time is not None and snap["synced_at"] == oldest_time:
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


def cmd_prune(args: argparse.Namespace) -> int:
    """Re-sync one or more owners and drop repos that 404 upstream.

    A full sync of every synced owner is compared against the current-state
    table: any tracked repo whose ``full_name`` was not in a successful
    re-fetch has been deleted or renamed on GitHub and is pruned.  History
    rows are kept (a pruned repo's snapshots remain valid data for its
    lifetime).  Per-owner API errors abort before anything is removed — a
    failed fetch can't prove a repo is gone, and pruning on incomplete data
    would delete live repos.  With ``--dry-run`` the list of gone repos is
    printed without deleting.

    Exit codes: 0 on success (including "nothing to prune"), 1 on an API
    error or an empty db.
    """
    owners = args.owners
    token = os.environ.get("GH_TOKEN")
    seen: set[str] = set()
    for owner in owners:
        try:
            raw = list_repos(owner, token=token)
        except APIError as exc:
            print(f"error: {owner}: {exc}", file=sys.stderr)
            return 1
        for repo in [normalize_repo(r) for r in raw]:
            seen.add(repo["full_name"])

    conn = _open_db(args.db)
    try:
        gone = get_gone_repos(conn, seen)
        if not gone and not get_repos(conn):
            print(f"no repos in {args.db} — run 'repo-radar sync <owner>' first")
            return 1
        if not gone:
            print("pruned 0 repo(s); everything tracked is still on GitHub")
            return 0
        for row in gone:
            print(f"gone: {row['full_name']} "
                  f"(last seen {row['stars']} stars, {row['forks']} forks)")
        if args.dry_run:
            print(f"{len(gone)} repo(s) to prune (dry run; nothing removed)")
            return 0
        removed = remove_repos(conn, [r["full_name"] for r in gone])
    finally:
        conn.close()
    print(f"pruned {removed} repo(s) from {args.db}")
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


def cmd_watch(args: argparse.Namespace) -> int:
    """Sync owners, then print only repos whose stars/forks moved.

    The watch is a sync with a diff: each owner's current repo list is
    fetched and upserted into the db exactly like ``sync`` (history rows
    included), and afterwards only the repos whose star or fork count
    differs from their previous snapshot are printed — a quiet run for a
    cron job is a single line.  On the first sync of a repo (no prior
    history row) it is reported as new; the same counts on a re-sync of an
    already-tracked repo are silent, which is what makes repeated runs
    useful.  With ``--json`` the changed repos come out as a list of
    objects carrying old/new counts and per-field deltas.

    Exit codes: 0 when every owner fetched (including "no changes"),
    1 when at least one owner's fetch failed.
    """
    owners: list[str] = args.owners
    token = os.environ.get("GH_TOKEN")

    # Fetch + upsert per owner, exactly like sync_many — but remember each
    # repo's count *before* this run so the diff is vs the prior snapshot.
    failures = 0
    fresh: dict[str, dict[str, Any]] = {}  # full_name -> normalized row
    for owner in owners:
        try:
            raw = list_repos(owner, token=token)
        except APIError as exc:
            print(f"error: {owner}: {exc}", file=sys.stderr)
            failures += 1
            continue

        conn = _open_db(args.db)
        try:
            rows = [normalize_repo(r) for r in raw]
            cursor = conn.execute(
                "SELECT full_name, stars, forks FROM history "
                "WHERE id IN (SELECT MAX(id) FROM history GROUP BY full_name)"
            )
            prev: dict[str, tuple[int, int]] = {
                row["full_name"]: (row["stars"], row["forks"])
                for row in cursor.fetchall()
            }
            upsert_repos(conn, rows)
        finally:
            conn.close()

        for row in rows:
            fn = row["full_name"]
            fresh[fn] = {**row, "_prev": prev.get(fn)}

    if not fresh and not failures:
        print("no repos fetched")
        return 1 if failures else 0

    changes: list[tuple[str, int, int, int, int, bool]] = []
    for fn in sorted(fresh):
        row = fresh[fn]
        old = row.pop("_prev")
        if old is None:
            # first snapshot of this repo: report it as new
            changes.append((fn, 0, 0, row["stars"], row["forks"], True))
        elif row["stars"] != old[0] or row["forks"] != old[1]:
            changes.append(
                (fn, old[0], old[1], row["stars"], row["forks"], False)
            )

    if args.json:
        payload = [
            {
                "full_name": fn,
                "new": is_new,
                "old_stars": os_,
                "old_forks": of_,
                "stars": ns,
                "forks": nf,
                "delta_stars": ns - os_,
                "delta_forks": nf - of_,
            }
            for fn, os_, of_, ns, nf, is_new in changes
        ]
        print(json.dumps(payload, indent=2))
        return 1 if failures else 0

    if not changes:
        print("no changes since last snapshot")
        return 1 if failures else 0

    for fn, os_, of_, ns, nf, is_new in changes:
        if is_new:
            print(f"new      {ns:<6} {nf:<5} {fn}")
        else:
            s_d, f_d = ns - os_, nf - of_
            s_str = _delta(s_d) if s_d else "0"
            f_str = _delta(f_d) if f_d else "0"
            print(f"changed  stars {ns:<6}({s_str:>4})  forks {nf:<5}({f_str:>4})  {fn}")

    return 1 if failures else 0


def cmd_top_gainers(args: argparse.Namespace) -> int:
    """Rank repos by stars gained since their first tracked snapshot.

    Baseline per repo is its *first* history row — the count it had when
    tracking of that repo started — so the table answers "which repos are
    quietly accumulating stars?" across heterogeneous tracking start
    dates.  With ``--since DAYS`` the baseline moves to the oldest
    snapshot inside the window, and a repo first tracked within the window
    shows its current star count as the gain (it had 0 stars at tracking
    start, by definition).

    Exit codes: 0 on success (including an empty db), 1 when the db is empty.
    """
    since = None
    if args.since is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=args.since)
        since = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = _open_db(args.db)
    try:
        latest: dict[str, dict[str, Any]] = {}
        baseline: dict[str, dict[str, Any]] = {}
        if since is None:
            latest = {r["full_name"]: r for r in get_latest_snapshots(conn)}
            baseline = {r["full_name"]: r for r in get_first_snapshots(conn)}
            # a repo first tracked after tracking "started" for others has
            # no older row to regress to: its gain is everything it has
        else:
            oldest_in_window: dict[str, dict[str, Any]] = {}
            rows = get_history(conn, full_name=None, since=since)
            for snap in rows:  # newest first from get_history
                fn = snap["full_name"]
                if fn not in latest:
                    latest[fn] = snap
                # rows are newest-first, so the LAST row we see for a repo is
                # its oldest snapshot inside the window — that is the baseline.
                oldest_in_window[fn] = snap
            baseline = oldest_in_window

        entries = []
        for fn in sorted(latest):
            # a repo first tracked after tracking of others started has no
            # older row: its whole current count is the gain.
            base_stars = baseline[fn]["stars"] if fn in baseline else 0
            entries.append({
                "full_name": fn,
                "stars_now": latest[fn]["stars"],
                "stars_base": base_stars,
                "gained": latest[fn]["stars"] - base_stars,
            })
        entries.sort(key=lambda e: (-e["gained"], e["full_name"]))
    finally:
        conn.close()

    if args.json:
        print(json.dumps(entries[: args.n], indent=2))
        return 0 if entries else 1
    if not entries:
        print(f"no repos in {args.db} — run 'repo-radar sync <owner>' first")
        return 1

    header = f"{'GAINED':>7} {'STARS':>7} REPO"
    print(header)
    print("-" * len(header))
    for e in entries[: args.n]:
        print(f"{e['gained']:>+7} {e['stars_now']:>7} {e['full_name']}")
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    """Compare two named snapshots: added / removed / moved repos.

    Each argument is a snapshot reference — an exact stored timestamp, a
    1-based index into the distinct sync times (oldest first), or ``first``
    / ``last``.  OLD must resolve to a snapshot at or before NEW; a bad
    reference exits 2 with the valid references listed.

    Output sections:

    * **added** — repos tracked at NEW but not yet at OLD (with their count
      as of NEW);
    * **removed** — repos tracked at OLD whose last snapshot is at or before
      OLD's time (i.e. gone from the db by NEW);
    * **moved** — surviving repos whose stars or forks differ between the
      two snapshots, with per-field deltas.

    A no-change diff prints a single line and exits 0; an empty db exits 1.
    """
    conn = _open_db(args.db)
    try:
        if not get_repos(conn) and not get_history(conn):
            print(f"no repos in {args.db} — run 'repo-radar sync <owner>' first")
            return 1
        t_old = resolve_snapshot_time(conn, args.old)
        t_new = resolve_snapshot_time(conn, args.new)
        if t_old is None or t_new is None:
            times = list_snapshot_times(conn)
            refs = "first, last, " + ", ".join(str(i + 1) for i in range(len(times))) \
                + ", or an exact timestamp"
            bad = args.old if t_old is None else args.new
            print(f"error: {bad!r} is not a stored snapshot ({refs})",
                  file=sys.stderr)
            for i, t in enumerate(times):
                print(f"  {i + 1}: {t}", file=sys.stderr)
            return 2
        if t_old > t_new:
            print(f"error: OLD {t_old} is after NEW {t_new} — swap the arguments",
                  file=sys.stderr)
            return 2
        if t_old == t_new:
            # an empty (OLD, NEW] window would otherwise report every tracked
            # repo as removed; two identical references are trivially quiet.
            print(f"no changes between {t_old} and {t_new}")
            return 0

        old_state = snapshots_at_or_before(conn, t_old)
        # The NEW side of the diff must reflect state *as of NEW*, where a repo
        # counts as present only if it has a snapshot inside (OLD, NEW].  A
        # repo whose last row is stale (before OLD) is not there at NEW even
        # though ``synced_at <= NEW`` would match its old row — that staleness
        # is precisely what "removed" means in this window.
        new_state = snapshots_between(conn, t_old, t_new)

        added = [fn for fn in new_state if fn not in old_state]
        removed = sorted(fn for fn in old_state if fn not in new_state)

        moved: list[tuple[str, dict[str, Any], dict[str, Any], int, int]] = []
        for fn in sorted(set(old_state) & set(new_state)):
            o, n = old_state[fn], new_state[fn]
            d_s, d_f = n["stars"] - o["stars"], n["forks"] - o["forks"]
            if d_s or d_f:
                moved.append((fn, o, n, d_s, d_f))

        payload: _DiffPayload = {
            "old": t_old,
            "new": t_new,
            "added": [
                {"full_name": fn, "stars": new_state[fn]["stars"],
                 "forks": new_state[fn]["forks"]}
                for fn in sorted(added)
            ],
            "removed": [
                {"full_name": fn, "stars": old_state[fn]["stars"],
                 "forks": old_state[fn]["forks"]}
                for fn in sorted(removed)
            ],
            "moved": [
                {"full_name": fn,
                 "old_stars": o["stars"], "stars": n["stars"],
                 "delta_stars": d_s,
                 "old_forks": o["forks"], "forks": n["forks"],
                 "delta_forks": d_f}
                for fn, o, n, d_s, d_f in moved
            ],
        }
    finally:
        conn.close()

    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    if not payload["added"] and not payload["removed"] and not payload["moved"]:
        print(f"no changes between {t_old} and {t_new}")
        return 0
    print(f"# diff {t_old} -> {t_new}")
    for item in payload["added"]:
        print(f"+ {item['full_name']}  stars {item['stars']:<6} "
              f"forks {item['forks']}")
    for item in payload["removed"]:
        print(f"- {item['full_name']}  (last seen {t_old}: "
              f"{item['stars']} stars, {item['forks']} forks)")
    for m in payload["moved"]:
        print(f"~ {m['full_name']}  stars {m['old_stars']} -> {m['stars']} "
              f"({_delta(m['delta_stars'])})  forks {m['old_forks']} -> {m['forks']} "
              f"({_delta(m['delta_forks'])})")
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
    p_sync.add_argument("--private", action="store_true",
                        help="also fetch private repos (requires GH_TOKEN with "
                             "repo scope; uses type=all so unscoped tokens 404)")
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
    p_report.add_argument("--since", type=int, metavar="DAYS", default=None,
                          help="measure growth vs the oldest snapshot within "
                               "the last DAYS days instead of all time")
    p_report.set_defaults(func=cmd_report)

    p_export = sub.add_parser(
        "export", help="dump the current snapshot as CSV and/or JSON")
    p_export.add_argument("--csv", metavar="PATH", default=None,
                          help="write a CSV file to PATH ('-' for stdout); "
                               "JSON is always printed on stdout")
    p_export.add_argument("--db", default=DEFAULT_DB,
                          help=f"SQLite db path (default {DEFAULT_DB})")
    p_export.set_defaults(func=cmd_export)

    p_prune = sub.add_parser(
        "prune", help="drop repos no longer on GitHub (404 on re-fetch)")
    p_prune.add_argument("owners", nargs="+", metavar="OWNER",
                         help="owners to re-check; tracked repos not in their "
                              "current repo lists are considered gone")
    p_prune.add_argument("--dry-run", action="store_true",
                         help="list the repos that would be pruned without removing them")
    p_prune.add_argument("--db", default=DEFAULT_DB,
                         help=f"SQLite db path (default {DEFAULT_DB})")
    p_prune.set_defaults(func=cmd_prune)

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

    p_watch = sub.add_parser(
        "watch", help="sync owners and print only repos whose stars/forks moved")
    p_watch.add_argument("owners", nargs="+", metavar="OWNER",
                         help="GitHub org or user name; repeat to watch several")
    p_watch.add_argument("--db", default=DEFAULT_DB,
                         help=f"SQLite db path (default {DEFAULT_DB})")
    p_watch.add_argument("--json", action="store_true",
                         help="print changed repos as a JSON list "
                              "(old/new counts + per-field deltas)")
    p_watch.set_defaults(func=cmd_watch)

    p_gainers = sub.add_parser(
        "top-gainers", help="rank repos by stars gained since first tracked")
    p_gainers.add_argument("n", nargs="?", type=int, default=10,
                           help="how many rows to print (default 10)")
    p_gainers.add_argument("--since", type=int, metavar="DAYS", default=None,
                           help="baseline = oldest snapshot within the last "
                                "DAYS days instead of each repo's first")
    p_gainers.add_argument("--db", default=DEFAULT_DB)
    p_gainers.add_argument("--json", action="store_true",
                           help="print a JSON list (gained/stars_now/stars_base)")
    p_gainers.set_defaults(func=cmd_top_gainers)

    p_diff = sub.add_parser(
        "diff", help="compare two snapshots: added / removed / moved repos")
    p_diff.add_argument("old",
                        help="older snapshot ref: 'first', 'last', 1-based "
                             "index, or an exact stored timestamp")
    p_diff.add_argument("new",
                        help="newer snapshot ref (same forms as OLD)")
    p_diff.add_argument("--db", default=DEFAULT_DB)
    p_diff.add_argument("--json", action="store_true",
                        help="print the diff as a JSON document "
                             "{old, new, added, removed, moved}")
    p_diff.set_defaults(func=cmd_diff)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    func = args.func
    assert callable(func), "subparser missing its handler"
    return int(func(args))


if __name__ == "__main__":
    sys.exit(main())
