"""Tests for the ``top-gainers`` and ``diff`` subcommands.

Both commands are pure readers over the history table, so tests drive a
throwaway db in a temp dir with deterministic upserts (monkeypatched
``list_repos`` for sync-driven scenarios, direct storage calls where the
CLI surface isn't the point).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from reporadar import main as cli  # noqa: E402
from reporadar.storage import connect, upsert_repos  # noqa: E402


def _row(name: str, owner: str = "org", stars: int = 0, forks: int = 0,
         language: str = "Python"):
    return {
        "full_name": f"{owner}/{name}",
        "name": name,
        "owner": owner,
        "description": None,
        "language": language,
        "stars": stars,
        "forks": forks,
        "open_issues": 0,
        "archived": False,
        "created_at": "2025-06-01T00:00:00Z",
    }


def _seed(db: str, snapshots: list[tuple[str, dict[str, int]]]) -> None:
    """Append one snapshot per entry; each is (synced_at, {name: stars}).

    Every row carries its explicit timestamp, so the wall clock never leaks
    into test data and ``--since`` windows stay deterministic regardless of
    when the suite runs.
    """
    conn = connect(db)
    try:
        for synced_at, counts in snapshots:
            rows = [_row(name, stars=stars)
                    for name, stars in counts.items()]
            upsert_repos(conn, rows, synced_at=synced_at)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# top-gainers
# ---------------------------------------------------------------------------

def test_top_gainers_ranks_by_stars_gained(tmp_path, capsys):
    db = str(tmp_path / "g.db")
    _seed(db, [
        ("2026-10-01T00:00:00Z", {"alpha": 50, "beta": 5}),
        ("2026-10-15T00:00:00Z", {"alpha": 60, "beta": 40}),
    ])

    rc = cli.main(["top-gainers", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    # beta gained +35, alpha +10: beta first
    lines = [l for l in out.splitlines() if "org/" in l]
    assert len(lines) == 2
    assert "org/beta" in lines[0] and "+35" in lines[0]
    assert "org/alpha" in lines[1] and "+10" in lines[1]


def test_top_gainers_pruned_repo_shows_growth_to_last_snapshot(tmp_path, capsys):
    """A repo dropped from a later sync keeps its growth (baseline->last)."""
    db = str(tmp_path / "g.db")
    _seed(db, [
        ("2026-10-01T00:00:00Z", {"alpha": 50, "beta": 5}),
        ("2026-10-15T00:00:00Z", {"alpha": 60, "gamma": 3}),  # beta gone
    ])

    rc = cli.main(["top-gainers", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    lines = [l for l in out.splitlines() if "org/" in l]
    # beta: baseline 5 (first row), last snapshot 5 -> +0; alpha +10 ranks first
    assert "org/alpha" in lines[0] and "+10" in lines[0]
    assert any("org/beta" in l and "+0" in l for l in lines)


def test_top_gainers_repo_tracked_later_counts_full_stars(tmp_path, capsys):
    db = str(tmp_path / "g.db")
    _seed(db, [
        ("2026-10-01T00:00:00Z", {"alpha": 10}),
        ("2026-10-15T00:00:00Z", {"alpha": 12}),
        ("2026-10-25T00:00:00Z", {"alpha": 14, "newbie": 12}),
        ("2026-10-28T00:00:00Z", {"alpha": 14, "newbie": 18}),
    ])

    rc = cli.main(["top-gainers", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    lines = [l for l in out.splitlines() if "org/" in l]
    # newbie was first tracked mid-history: its growth (first row -> latest)
    # is 6, still topping alpha's +4.
    assert "org/newbie" in lines[0] and "+6" in lines[0]
    assert "org/alpha" in lines[1] and "+4" in lines[1]


def test_top_gainers_n_limits_rows(tmp_path, capsys):
    db = str(tmp_path / "g.db")
    _seed(db, [
        ("2026-10-01T00:00:00Z", {"a": 0, "b": 0, "c": 0}),
        ("2026-10-15T00:00:00Z", {"a": 3, "b": 2, "c": 1}),
    ])

    rc = cli.main(["top-gainers", "1", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    lines = [l for l in out.splitlines() if "org/" in l]
    assert len(lines) == 1 and lines[0].lstrip().startswith("+3")


def test_top_gainers_json_shape(tmp_path, capsys):
    db = str(tmp_path / "g.db")
    _seed(db, [
        ("2026-10-01T00:00:00Z", {"alpha": 5}),
        ("2026-10-15T00:00:00Z", {"alpha": 9}),
    ])

    rc = cli.main(["top-gainers", "--json", "--db", db])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload == [{
        "full_name": "org/alpha",
        "stars_now": 9,
        "stars_base": 5,
        "gained": 4,
    }]


def test_top_gainers_empty_db_exits_1(tmp_path, capsys):
    db = str(tmp_path / "empty.db")
    rc = cli.main(["top-gainers", "--db", db])
    out = capsys.readouterr().out
    assert rc == 1
    assert "no repos in" in out


def test_top_gainers_since_windows_baseline(tmp_path, capsys, monkeypatch):
    # Pin the clock: --since windows are computed from "now", and the
    # fixture dates (2026-09-20 / 2026-10-28) must sit on opposite sides of
    # both cutoffs for days after the test was written. A fixed "now" keeps
    # the test deterministic forever.
    from datetime import timedelta, timezone as _tz

    class _FixedDT:
        @classmethod
        def now(cls, tz=None):
            return _tz.utc and _FixedDT._now()

        @classmethod
        def _now(cls):
            from datetime import datetime as _real
            return _real(2026, 10, 29, 12, 0, 0, tzinfo=_tz.utc)

    monkeypatch.setattr(cli, "datetime", type("_Patched", (_FixedDT,), {
        "timedelta": timedelta,
        "timezone": _tz,
    }))

    db = str(tmp_path / "g.db")
    _seed(db, [
        ("2026-09-20T00:00:00Z", {"alpha": 100}),
        ("2026-10-28T00:00:00Z", {"alpha": 130}),
    ])

    # window covers only the 2026-10-28 snapshot: baseline inside the window
    # is the current count, so gain = 0 (no movement in-window).
    rc = cli.main(["top-gainers", "--since", "5", "--json", "--db", db])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload[0]["gained"] == 0
    assert payload[0]["stars_base"] == 130

    # a wider window reaches back to the 2026-09-20 row: gain = 130 - 100.
    rc = cli.main(["top-gainers", "--since", "45", "--json", "--db", db])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    entry = [e for e in payload if e["full_name"] == "org/alpha"][0]
    assert entry["stars_base"] == 100
    assert entry["gained"] == 30


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------

def _three_snapshots(db: str) -> None:
    """t1: alpha(5) beta(2); t2: alpha(8); t3: alpha(9) gamma(4).

    beta is tracked only at t1 (pruned before t2): the classic "removed" case.
    gamma appears at t3, the final snapshot — added in a first->last diff.
    Removals are judged inside the snapshot window — whether a repo was
    tracked at OLD and has no snapshot after OLD's time — not against the
    live current-state table (see test_diff_removal_is_judged_in_window_
    not_live_state for that case directly).
    """
    _seed(db, [
        ("2026-10-01T00:00:00Z", {"alpha": 5, "beta": 2}),
        ("2026-10-08T00:00:00Z", {"alpha": 8}),
        ("2026-10-15T00:00:00Z", {"alpha": 9, "gamma": 4}),
    ])


def test_diff_added_removed_moved(tmp_path, capsys):
    db = str(tmp_path / "d.db")
    _three_snapshots(db)

    rc = cli.main(["diff", "first", "last", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    assert "+ org/gamma" in out        # added after the first snapshot
    assert "- org/beta" in out         # tracked at first, gone by last
    assert "- org/gamma" not in out    # gamma survives to NEW: not removed
    assert "~ org/alpha" in out        # stars moved between the snapshots
    assert "5 -> 9" in out and "+4" in out


def test_diff_same_snapshot_is_quiet(tmp_path, capsys):
    db = str(tmp_path / "d.db")
    _three_snapshots(db)

    rc = cli.main(["diff", "1", "1", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no changes between" in out


def test_diff_reversed_order_exits_2(tmp_path, capsys):
    db = str(tmp_path / "d.db")
    _three_snapshots(db)

    rc = cli.main(["diff", "last", "first", "--db", db])
    err = capsys.readouterr().err
    assert rc == 2
    assert "swap the arguments" in err


def test_diff_bad_reference_exits_2_and_lists_valid(tmp_path, capsys):
    db = str(tmp_path / "d.db")
    _three_snapshots(db)

    rc = cli.main(["diff", "99", "last", "--db", db])
    err = capsys.readouterr().err
    assert rc == 2
    assert "'99' is not a stored snapshot" in err
    # valid references are listed for discovery
    assert "first, last, 1, 2, 3" in err
    assert "2026-10-01T00:00:00Z" in err


def test_diff_json_payload(tmp_path, capsys):
    db = str(tmp_path / "d.db")
    # dedicated fixture: gamma is added between t1 and t2; beta (tracked at
    # t1, gone by t2) proves removed is judged in the window.
    _seed(db, [
        ("2026-10-01T00:00:00Z", {"alpha": 5, "beta": 2}),
        ("2026-10-08T00:00:00Z", {"alpha": 8, "gamma": 4}),
    ])

    rc = cli.main(["diff", "first", "last", "--json", "--db", db])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["old"] == "2026-10-01T00:00:00Z"
    assert payload["new"] == "2026-10-08T00:00:00Z"
    assert payload["added"] == [
        {"full_name": "org/gamma", "stars": 4, "forks": 0}
    ]
    # beta's last snapshot is t1 itself (pruned before t2): it counts as
    # removed in this window.
    assert payload["removed"] == [
        {"full_name": "org/beta", "stars": 2, "forks": 0}
    ]
    assert len(payload["moved"]) == 1
    moved = payload["moved"][0]
    assert moved["full_name"] == "org/alpha"
    assert moved["old_stars"] == 5 and moved["stars"] == 8
    assert moved["delta_stars"] == 3


def test_diff_exact_timestamps_resolve(tmp_path, capsys):
    db = str(tmp_path / "d.db")
    _three_snapshots(db)

    rc = cli.main(["diff", "2026-10-01T00:00:00Z",
                   "2026-10-08T00:00:00Z", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    assert "# diff 2026-10-01T00:00:00Z -> 2026-10-08T00:00:00Z" in out
    # alpha moved between these two snapshots; gamma arrives at t3, outside
    # this window.
    assert "~ org/alpha" in out and "5 -> 8" in out


def test_diff_empty_db_exits_1(tmp_path, capsys):
    db = str(tmp_path / "empty.db")
    rc = cli.main(["diff", "first", "last", "--db", db])
    out = capsys.readouterr().out
    assert rc == 1
    assert "no repos in" in out


def test_diff_removal_is_judged_in_window_not_live_state(tmp_path, capsys):
    """A repo pruned *after* NEW was already absent from the window: added."""
    db = str(tmp_path / "d.db")
    # t1: alpha only. t2: gamma appears. t3: gamma's final snapshot (then it
    # is pruned — history kept, current-state row gone).
    _seed(db, [
        ("2026-10-01T00:00:00Z", {"alpha": 5}),
        ("2026-10-08T00:00:00Z", {"alpha": 8, "gamma": 4}),
        ("2026-10-15T00:00:00Z", {"alpha": 9, "gamma": 5}),
    ])
    conn = connect(db)
    try:
        from reporadar.storage import remove_repos
        assert remove_repos(conn, ["org/gamma"]) == 1
    finally:
        conn.close()

    # diff t1 -> t2: gamma was not tracked at OLD and has no snapshot after
    # t1's window edge (t1), so the in-window removal rule doesn't fire —
    # it is "added", even though its current-state row is already pruned.
    rc = cli.main(["diff", "first", "2", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    assert "+ org/gamma" in out
    assert "- org/gamma" not in out


def test_diff_fork_movement_reports_both_fields(tmp_path, capsys):
    db = str(tmp_path / "d.db")
    conn = connect(db)
    try:
        upsert_repos(conn, [_row("alpha", stars=1, forks=0)],
                     synced_at="2026-10-01T00:00:00Z")
        upsert_repos(conn, [_row("alpha", stars=1, forks=3)],
                     synced_at="2026-10-08T00:00:00Z")
    finally:
        conn.close()

    rc = cli.main(["diff", "first", "last", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    line = [l for l in out.splitlines() if "org/alpha" in l][0]
    assert "0 -> 3" in line and "+3" in line
