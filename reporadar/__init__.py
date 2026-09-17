"""repo-radar: snapshot an org's GitHub repos into a local SQLite db.

Each run fetches the org's public repositories from the GitHub REST API,
normalizes them into flat rows, and upserts them into a local SQLite
database keyed by repo full name.  The ``history`` table keeps one row per
snapshot (timestamped), so you can track stars / forks / open-issues over
time without re-querying the API.

Only stdlib is used: ``urllib`` for HTTP, ``sqlite3`` for storage.
"""

__version__ = "0.1.0"
