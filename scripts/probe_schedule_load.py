"""Break down where POST /api/schedule/fetch spends its time.

The endpoint does six things per call, and only ONE of them is genuinely live
(betting lines). This prints each phase's cost plus a cProfile of the whole call,
so we can decide which parts move to the weekly artifact and which stay live.
"""
import cProfile
import io
import os
import pstats
import sys
import time

os.environ.setdefault("REFRESH_INTERVAL_SECONDS", "0")
os.environ["D1_WRITE_ENABLED"] = "0"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402

YEAR, WEEK = 2026, 4

print("=== phase timing (each phase called directly, cold caches) ===")


def phase(label, fn):
    t0 = time.time()
    try:
        out = fn()
        n = len(out) if hasattr(out, "__len__") else "-"
        print("   %-34s %7.2fs   (%s items)" % (label, time.time() - t0, n))
        return out
    except Exception as e:
        print("   %-34s FAILED  %s: %s" % (label, type(e).__name__, e))
        return None


app._CFBD_GAMES_CACHE.clear()
phase("CFBD season games (shared)", lambda: app._cfbd_season_games(YEAR))
phase("fetch_cfbd_schedule(week)", lambda: app.fetch_cfbd_schedule(WEEK, YEAR))
phase("_build_team_map()", app._build_team_map)
phase("load_fbs_conferences()", app.load_fbs_conferences)
phase("_load_active_injuries()", app._load_active_injuries)
phase("_cfbd_weather(week)", lambda: app._cfbd_weather(WEEK, YEAR))
app._odds_cache.clear()
phase("_fetch_odds_map()  <-- the live one", app._fetch_odds_map)
app._odds_cache.clear()
phase("_fetch_odds_map() again (cache effect)", app._fetch_odds_map)

print("\n=== whole endpoint, cold schedule cache ===")
app._SCHEDULE_FETCH_CACHE.clear()
t0 = time.time()
out = app.api_schedule_fetch(week=WEEK, year=YEAR)
print("   api_schedule_fetch week=%d  %.2fs  (%d matchups)"
      % (WEEK, time.time() - t0, len(out.get("matchups", []))))

print("\n=== cProfile of the same call ===")
app._SCHEDULE_FETCH_CACHE.clear()
pr = cProfile.Profile()
pr.enable()
app.api_schedule_fetch(week=WEEK, year=YEAR)
pr.disable()
s = io.StringIO()
pstats.Stats(pr, stream=s).sort_stats("cumulative").print_stats(14)
print(s.getvalue())