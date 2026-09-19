# repo-radar

Snapshot an org's (or user's) public GitHub repos into a local **SQLite**
database — then track stars, forks, open issues, and language mix over time
without re-querying the API.

The motivation: GitHub's REST API is great for a one-off check, but if you
want "how much has this org grown this quarter?" or "which of their repos is
quietly accumulating stars?", you want a local copy with history.  repo-radar
keeps two tables: a current-state table (one row per repo, upserted on every
sync) and an append-only snapshot history (one row per repo per sync), so
deltas are a one-line query later.

Stdlib only — no `requests`, no `SQLAlchemy`, no API key required for public
repos (though `GH_TOKEN` raises the rate limit from 60 to 5,000 req/hr).

## Install

```bash
git clone https://github.com/SkimMilkSwag/repo-radar
cd repo-radar
pip install -e .        # puts `repo-radar` on your PATH
```

Or skip the install and run from source: `python -m reporadar.main <cmd>`.

## Usage

```bash
# Snapshot an org's public repos into ./repo_radar.db
export GH_TOKEN=ghp_...          # optional; raises rate limits
repo-radar sync SkimMilkSwag
# synced 9 repo(s) for SkimMilkSwag: 142 stars, 31 forks (Python:7, Go:1, None:1)

# Most-starred tracked repos
repo-radar top 5

# How has one repo's star count moved across your snapshots?
repo-radar history SkimMilkSwag/repo-radar
# 2026-09-17T03:12:44Z  stars 0       +0  forks 0      +0
```

`history` prints newest-first and shows `+N`/`-N` deltas vs the previous
snapshot — the first snapshot has no delta by definition.

### Options

| Command | Flags | Notes |
|---------|-------|-------|
| `sync OWNER...` | `--db PATH` | db defaults to `./repo_radar.db`; one or more owners per run, all merged into the same db |
| `top [N]` | `--db PATH`, `--json` | N defaults to 10; `--json` prints a JSON list of repos |
| `history REPO` | `--limit N`, `--db PATH`, `--json` | N defaults to 20 snapshots; `--json` prints snapshots with delta fields |
| `report` | `--db PATH` | markdown table: per-language totals + growth since the oldest snapshot |

## Using the db directly

The schema is deliberately boring — two tables, plain columns:

```sql
sqlite3 repo_radar.db

-- total stars per language, current state
SELECT COALESCE(language, '(none)') AS lang, SUM(stars)
FROM repos GROUP BY lang ORDER BY 2 DESC;

-- star growth of one repo between your snapshots
SELECT synced_at, stars,
       stars - LAG(stars) OVER (ORDER BY synced_at) AS delta
FROM history WHERE full_name = 'owner/name';
```

Run `sync` on a schedule (cron, launchd, whatever) and the history table
fills in one row per repo per run.

## Library use

The CLI is thin over three importable modules:

```python
from reporadar.client import list_repos
from reporadar.normalize import normalize_repo, summarize
from reporadar.storage import connect, upsert_repos

repos = [normalize_repo(r) for r in list_repos("SkimMilkSwag")]
conn = connect("my.db")
upsert_repos(conn, repos)      # current state + history, one transaction
print(summarize(repos))
```

## Tests

Offline: the network layer is monkeypatched and every test uses a temp db.

```bash
python -m pytest tests/ -q
# 24 passed
```

## License

MIT — see [LICENSE](LICENSE).
