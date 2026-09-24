"""Post-deploy verification for v37: prod smoke + is the CONTAINER writing weather?

Two separate claims, both checked against reality:
  1. the site is healthy on v37
  2. the container's own scheduler is appending to weather_snapshots -- i.e. the
     series grows without anyone visiting a page

A row written by a local test is NOT evidence for (2): it has to carry a poll_ts
from after the deploy. This script therefore compares the newest poll_ts against
the boot time rather than just counting rows.
"""
import json
import os
import sys
import urllib.request

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
import d1_store  # noqa: E402

P = "https://skeezcfb-rankings.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")


def get(path):
    req = urllib.request.Request(P + path, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.status, json.loads(r.read().decode("utf-8", "replace"))


print("=== 1. prod smoke ===")
for p in ["/", "/analytics", "/schedule", "/win-totals"]:
    try:
        st, _ = get(p)
        print("   %-12s %s" % (p, st))
    except Exception as e:
        print("   %-12s FAIL %s" % (p, e))
try:
    _, h = get("/api/health")
    print("   build=%s  model_version=%s  degraded=%s"
          % (h.get("build"), h.get("model_version"), h.get("degraded")))
except Exception as e:
    print("   /api/health FAIL", e)

print("\n=== 2. weather_snapshots in D1 ===")
r = d1_store.query(
    "SELECT COUNT(*) AS n, COUNT(DISTINCT game_id) AS games, "
    "COUNT(DISTINCT poll_ts) AS polls FROM weather_snapshots")
print("   totals:", r[0] if r else "?")

polls = d1_store.query(
    "SELECT poll_ts, COUNT(*) AS n FROM weather_snapshots "
    "GROUP BY poll_ts ORDER BY poll_ts DESC LIMIT 6")
print("   polls (newest first):")
for p in polls:
    print("     %s  %s rows" % (p["poll_ts"], p["n"]))

# The deploy went live ~17:03Z; anything at/after that came from the container.
after = d1_store.query(
    "SELECT COUNT(*) AS n FROM weather_snapshots WHERE poll_ts >= '2026-09-24T17:00'")
n_after = after[0]["n"] if after else 0
print("\n   rows from poll_ts >= 17:00Z (container-written): %s" % n_after)
print("   VERDICT:", "CONTAINER IS WRITING WEATHER ✓" if n_after else
      "no container-written rows yet (scheduler may not have ticked)")