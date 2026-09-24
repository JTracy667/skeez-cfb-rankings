"""Verify the stored slate serves the SAME numbers, and serves them fast.

  1. Build a fresh slate (forced recompute), read it back from the durable store, and
     diff them game by game. A "fast" page that changes a number is a regression, not
     an optimisation -- so this compares every field of every matchup.
  2. Report build cost vs store-read cost.

Only volatile timestamps are ignored.
"""
import json
import os
import sys
import time

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)


def _load_env():
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

import app        # noqa: E402
import d1_store   # noqa: E402

wk, yr = app.live_week(), app.CFBD_YEAR
print("live week:", wk, "season:", yr)

fp = app._slate_fingerprint()
print("input fingerprint:", (fp or "")[:16], "...")

print("\n=== 1. force a fresh build (stores as a side effect) ===")
app._SLATE_FORCE.add((int(wk), yr))
app._SCHEDULE_FETCH_CACHE.pop((wk, yr), None)
t0 = time.time()
built = app.api_schedule_fetch(wk, yr)
t_build = time.time() - t0
print("   built+stored in %.2fs, %s matchups" % (t_build, len(built.get("matchups") or [])))

print("\n=== 2. read back from the store (the request path) ===")
app._SCHEDULE_FETCH_CACHE.pop((wk, yr), None)
t0 = time.time()
stored = app._slate_from_store(wk, yr)
t_read = time.time() - t0
print("   read in %.3fs" % t_read)
if not stored:
    print("   !! store returned nothing")
    raise SystemExit(1)

VOLATILE = ("updated", "generated", "built_at", "as_of", "cached", "note")


def strip(d):
    out = json.loads(json.dumps(d))
    for k in VOLATILE:
        out.pop(k, None)
    for k in list(out):
        if k.startswith("_store_"):
            out.pop(k, None)
    return out


a, b = strip(built), strip(stored)
diffs = []
if set(a) != set(b):
    diffs.append("top-level keys: %s vs %s" % (sorted(a), sorted(b)))
ga, gb = a.get("matchups") or [], b.get("matchups") or []
if len(ga) != len(gb):
    diffs.append("matchup count %s -> %s" % (len(ga), len(gb)))
else:
    for x, y in zip(ga, gb):
        if x != y:
            keys = sorted(set(x) | set(y))
            bad = [k for k in keys if x.get(k) != y.get(k)]
            diffs.append("game %s differs in %s" % (x.get("game_id") or x.get("home"), bad))

print("\n=== 3. equivalence ===")
if diffs:
    print("   MISMATCH (%d):" % len(diffs))
    for d in diffs[:8]:
        print("     -", d)
else:
    print("   IDENTICAL to a fresh compute (%d matchups, 0 field differences)" % len(ga))

print("\n   build %.2fs vs store-read %.3fs" % (t_build, t_read))
rows = d1_store.query("SELECT season, week, fingerprint, model_version, "
                      "LENGTH(payload_gz) AS gz FROM slate_cache")
print("   slate_cache rows:", rows)

print("\n=== 4. cold request path (what a visitor hits) ===")
app._SCHEDULE_FETCH_CACHE.clear()
app._SLATE_REBUILD_LAST["ts"] = time.time()      # suppress the background revalidate
t0 = time.time()
served = app.api_schedule_fetch(wk, yr)
t_cold = time.time() - t0
print("   api_schedule_fetch from cold memory: %.2fs, matchups=%s, cached=%s"
      % (t_cold, len(served.get("matchups") or []), served.get("cached")))
print("   VERDICT:", "SERVED FROM THE STORE ✓" if served.get("cached") and t_cold < 2
      else "did not use the store ✗")
print("   internal keys leaked?",
      [k for k in served if k.startswith("_store_")] or "none")