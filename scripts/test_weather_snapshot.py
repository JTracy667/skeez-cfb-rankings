"""End-to-end check of the weather stream: force a write, then READ IT BACK from D1.

A successful write call is not proof the rows landed — this queries the table and
prints what is actually stored, including the shape a backtest would consume.

Usage:  python scripts/test_weather_snapshot.py
"""
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)


def _load_env():
    """d1_store._token() reads the ENVIRONMENT only (the container gets the token via
    Worker envVars), so a local run must pull it out of .env."""
    try:
        with open(os.path.join(_REPO, ".env"), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() in ("CF_D1_TOKEN", "CF_D1_DB_ID", "CF_ACCOUNT_ID"):
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except OSError:
        pass


_load_env()
os.environ["D1_WRITE_ENABLED"] = "1"

import app            # noqa: E402
import d1_store       # noqa: E402

wk = app.live_week()
print("live_week() ->", wk)

# Force the hourly throttle open so this run actually attempts the write.
app._WX_LOCK["ts"] = 0.0
n = app._maybe_write_weather(wk)
print("snapshot_weather attempted:", n, "row(s)")

rows = d1_store.query(
    "SELECT COUNT(*) AS n, COUNT(DISTINCT game_id) AS games, "
    "COUNT(DISTINCT poll_ts) AS polls, MIN(poll_ts) AS first_poll, MAX(poll_ts) AS last_poll "
    "FROM weather_snapshots")
print("\n=== table state ===")
print(rows[0] if rows else "(query failed)")

sample = d1_store.query(
    "SELECT game_id, week, kickoff_utc, wind_mph, temp_f, condition, indoor "
    "FROM weather_snapshots ORDER BY id DESC LIMIT 6")
print("\n=== newest rows ===")
for r in sample:
    print("  ", r)

wr = d1_store.query(
    "SELECT COUNT(*) AS n FROM weather_snapshots WHERE wind_mph IS NOT NULL")
print("\nrows with wind:", wr[0]["n"] if wr else "?")