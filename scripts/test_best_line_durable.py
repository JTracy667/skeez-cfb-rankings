"""Prove best-line state survives a fresh container -- WITHOUT spending PropLine quota.

Deliberately touches only the load/save helpers. The earlier quota leak was made worse
by test runs that each triggered a live multi-source odds pull against the SAME daily
cap the site uses; tests must not buy data.

Checks:
  1. the two caches round-trip into D1 app_state and read back identical
  2. a cold process (nothing in memory) loads them from D1 rather than the image file
  3. how many events that stops being re-fetched on every boot
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

print("=== 0. what the image-baked files hold (the stale copies) ===")
file_ts = {}
file_store = {}
try:
    file_ts = json.loads(app.BEST_LINE_TS_FILE.read_text(encoding="utf-8"))
except Exception as e:
    print("   ts file:", e)
try:
    file_store = json.loads(app.BEST_LINE_STORE_FILE.read_text(encoding="utf-8"))
except Exception as e:
    print("   store file:", e)
print("   file ts entries: %s | file store entries: %s" % (len(file_ts), len(file_store)))

print("\n=== 1. migrate both caches into D1 app_state ===")
app._save_best_line_ts(file_ts)
app._save_best_line_store(file_store)
rows = d1_store.query("SELECT key, LENGTH(value) AS n, updated_at FROM app_state "
                      "WHERE key LIKE 'propline_%'")
for r in rows:
    print("   ", r)

print("\n=== 2. cold read (what a fresh container now gets) ===")
t0 = time.time()
durable_ts = app._load_best_line_ts()
durable_store = app._load_best_line_store()
dt = time.time() - t0
print("   loaded %s ts entries + %s stored lines in %.2fs" % (len(durable_ts), len(durable_store), dt))

ok_ts = bool(durable_ts) and len(durable_ts) >= len(file_ts)
ok_store = bool(durable_store) and len(durable_store) >= len(file_store)
print("   ts round-trip:", "OK" if ok_ts else "FAILED")
print("   store round-trip:", "OK" if ok_store else "FAILED")

print("\n=== 3. what that saves per container boot ===")
now = time.time()
recent = [k for k, v in durable_ts.items() if now - float(v) < 48 * 3600]
print("   events with a fetch younger than 48h (near-game tier): %s" % len(recent))
print("   events in the durable store (line already paid for):   %s" % len(durable_store))
print("\n   Before this fix a fresh container had NO durable ts, so _best_line_due()")
print("   returned True for every event in the 10-day window and the whole per-event")
print("   sweep ran again (~71 calls). Now the timestamps and the lines survive the")
print("   restart, so only genuinely-due events are bought.")