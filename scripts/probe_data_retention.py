"""Read-only: what is the live site ACTUALLY persisting for future backtests?

Answers Jeff's question from the database itself, not from intent:
  - which tables exist
  - how many rows each has, and its date span
  - whether the weather columns are actually populated (our only own-source
    weather history -- CFBD has no historical weather to backfill from)
"""
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)


def _load_env():
    """d1_store._token() reads the ENVIRONMENT only -- in the container the token
    arrives via Worker envVars, so a local run has to pull it out of .env here."""
    path = os.path.join(_REPO, ".env")
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                if k in ("CF_D1_TOKEN", "CF_D1_DB_ID", "CF_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN"):
                    os.environ.setdefault(k, v.strip().strip('"').strip("'"))
    except OSError:
        pass


_load_env()

import d1_store  # noqa: E402


def q(sql, timeout=90):
    try:
        return d1_store.query(sql, timeout=timeout)
    except Exception as e:
        print("      ! %s: %s" % (type(e).__name__, e))
        return []


print("=== tables ===")
tables = [r["name"] for r in q("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
print("   ", ", ".join(tables) if tables else "(none)")

print("\n=== row counts + span ===")
SPAN = {
    "odds_snapshots": "poll_ts",
    "closing_lines": "ts_utc",
    "rankings_daily": "date",
    "model_predictions": "created_at",
    "injury_snapshots": "created_at",
    "stat_observations": "season",
    "games": "season",
    "raw_payloads": "ts_utc",
    "backtest_runs": "ts_utc",
}
for t in sorted(SPAN):
    if t not in tables:
        print("   %-22s MISSING" % t)
        continue
    n = q("SELECT COUNT(*) AS n FROM %s" % t)
    n = n[0]["n"] if n else "?"
    col = SPAN[t]
    span = q("SELECT MIN(%s) AS a, MAX(%s) AS b FROM %s" % (col, col, t))
    span = ("%s .. %s" % (span[0]["a"], span[0]["b"])) if span else "-"
    print("   %-22s %10s rows   %s" % (t, n, span))

print("\n=== model_predictions: the weather we are capturing ===")
cols = q("PRAGMA table_info(model_predictions)")
names = [c["name"] for c in cols]
print("    columns:", ", ".join(names))
wx = [c for c in names if any(k in c for k in ("wind", "temp", "weather", "indoor"))]
if wx:
    sel = ", ".join("SUM(CASE WHEN %s IS NOT NULL THEN 1 ELSE 0 END) AS n_%s" % (c, c) for c in wx)
    r = q("SELECT COUNT(*) AS n_total, %s FROM model_predictions" % sel)
    if r:
        row = r[0]
        print("    rows: %s" % row.get("n_total"))
        for c in wx:
            print("      populated %-18s %s" % (c, row.get("n_" + c)))
else:
    print("    !! no weather columns found")

print("\n=== odds_snapshots: distinct poll timestamps (line-movement resolution) ===")
r = q("SELECT COUNT(DISTINCT poll_ts) AS polls, COUNT(*) AS rows_, MIN(poll_ts) AS a, MAX(poll_ts) AS b FROM odds_snapshots")
if r:
    print("    polls=%s  rows=%s  %s .. %s" % (r[0]["polls"], r[0]["rows_"], r[0]["a"], r[0]["b"]))