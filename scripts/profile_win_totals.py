"""Profile compute_win_totals to find where the ~40s of CPU actually goes."""
import cProfile
import io
import os
import pstats
import sys

os.environ.setdefault("REFRESH_INTERVAL_SECONDS", "0")
os.environ["D1_WRITE_ENABLED"] = "0"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402

app._cfbd_season_games(2026)          # prime the shared caches, as a boot would
app._WIN_TOTALS_CACHE.clear()

pr = cProfile.Profile()
pr.enable()
app.compute_win_totals(2026)
pr.disable()

s = io.StringIO()
pstats.Stats(pr, stream=s).sort_stats("cumulative").print_stats(18)
print(s.getvalue())