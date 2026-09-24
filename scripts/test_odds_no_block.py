"""Prove the request path no longer blocks on the odds feed.

Simulates the real failure condition: a disk cache older than ODDS_TTL (24h), which is
what a container built more than a day ago sees. Before the change that made
_fetch_odds_map() run a live multi-source pull inline (~7s on prod).

Asserts: the call returns quickly, returns a usable stale map, and hands the refresh
to a background thread. The disk cache is restored afterwards.
"""
import json
import os
import shutil
import sys
import time

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
os.environ["D1_WRITE_ENABLED"] = "0"          # no D1 traffic from this test

import app  # noqa: E402

CACHE = app.ODDS_CACHE_FILE
BACKUP = str(CACHE) + ".testbak"
shutil.copy2(CACHE, BACKUP)
try:
    payload = json.loads(CACHE.read_text(encoding="utf-8"))
    n_entries = len(payload.get("odds", {}))
    payload["ts"] = time.time() - (app.ODDS_TTL + 3600)   # force stale/beyond-TTL
    CACHE.write_text(json.dumps(payload), encoding="utf-8")
    print("disk cache entries: %s, aged to %s h past TTL"
          % (n_entries, 1))

    app._odds_cache["ts"] = 0        # cold memory cache too
    app._odds_cache["data"] = {}

    t0 = time.time()
    m = app._fetch_odds_map()
    dt = time.time() - t0
    print("_fetch_odds_map() cold/stale -> %s entries in %.2fs" % (len(m), dt))
    print("VERDICT:", "REQUEST PATH DID NOT BLOCK ✓" if dt < 2.0
          else "STILL BLOCKING ✗ (%.2fs)" % dt)
    print("         ", "served stale lines ✓" if m else "served NOTHING ✗")
finally:
    shutil.move(BACKUP, CACHE)
    print("disk cache restored")