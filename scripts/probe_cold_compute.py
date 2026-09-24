"""Time the win-totals / schedule recompute paths the way a COLD container sees them.

The container's caches are all in memory (win-totals 1h, cfbd games 30m, schedule
5m) and sleepAfter is 5m, so a cold container re-runs the full projection. This
script measures that compute so we can size it against the 1/4 vCPU instance.

Read-only: D1 writes are left OFF, the live refresh scheduler is disabled.
"""
import json
import os
import time

os.environ.setdefault("REFRESH_INTERVAL_SECONDS", "0")   # no live scheduler on import
os.environ["D1_WRITE_ENABLED"] = "0"                     # never write D1 from a probe
os.environ.setdefault("CFB_PUBLIC_URL", "http://127.0.0.1:1")  # nothing should call out to prod

import sys  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402

WARM = "--warm" in os.sys.argv

# 1) prime the shared CFBD caches (this is what a container boot pays anyway,
#    and cfbd_season_games has a disk fallback)
t0 = time.time()
games = app._cfbd_season_games(2026)
t_games = time.time() - t0
print("cfbd season games: %d games in %.2fs" % (len(games), t_games))

# 2) the win-totals recompute (memory cache cleared => the real path)
app._WIN_TOTALS_CACHE.clear()
t0 = time.time()
wt = app.compute_win_totals(2026)
t_wt = time.time() - t0
print("compute_win_totals (cold cache): %.2fs for %d teams" % (t_wt, len(wt.get("teams", []))))

# Dump the payload so a before/after code change can be proven to alter NOTHING
# but the clock (compare with: diff <a.json> <b.json>).
_dump = os.environ.get("PROBE_DUMP")
if _dump:
    with open(_dump, "w", encoding="utf-8") as fh:
        json.dump(wt, fh, sort_keys=True, indent=1)
    print("dumped payload -> %s" % _dump)

# 3) warm repeat, to show the cache effect
t0 = time.time()
app.compute_win_totals(2026)
print("compute_win_totals (warm cache): %.3fs" % (time.time() - t0))

# 4) the Schedule page loader for the live week
app._SCHEDULE_FETCH_CACHE.clear() if hasattr(app, "_SCHEDULE_FETCH_CACHE") else None
t0 = time.time()
try:
    sched = app.api_schedule_fetch(week=4, year=2026)
    print("api_schedule_fetch week=4 (cold cache): %.2fs, %d matchups"
          % (time.time() - t0, len(sched.get("matchups", []))))
except Exception as e:
    print("api_schedule_fetch failed: %s: %s" % (type(e).__name__, e))

print("\nscale note: container instance_type=basic is 1/4 vCPU -> multiply the "
      "compute numbers above by roughly 3-6x for the live cold path.")