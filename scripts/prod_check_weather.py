"""Is the DEPLOYED code writing weather? Force the prod path, then read D1 back.

The pages are HTML, so only /api/health may be parsed as JSON (an earlier version of
this check tried to json.loads the HTML and reported a false failure on every page).

The decisive test: POST the same endpoint the Schedule page's JS calls. That runs
_maybe_write_weather inside the LIVE container, so a new poll_ts appearing proves the
deployed code path works -- whereas a row written by a local run proves nothing.
"""
import json
import os
import sys
import time
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


def req(path, data=None):
    r = urllib.request.Request(
        P + path, data=data,
        headers={"User-Agent": UA, "Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(r, timeout=120) as resp:
        body = resp.read()
        return resp.status, body, time.time() - t0


def polls():
    return d1_store.query(
        "SELECT poll_ts, COUNT(*) AS n FROM weather_snapshots "
        "GROUP BY poll_ts ORDER BY poll_ts DESC LIMIT 5")


print("=== page statuses (HTML -- status only, never parsed as JSON) ===")
for p in ["/", "/analytics", "/schedule", "/win-totals", "/api/health"]:
    try:
        st, body, dt = req(p)
        marker = ""
        if p == "/api/health":
            h = json.loads(body.decode("utf-8", "replace"))
            marker = " build=%s degraded=%s" % (h.get("build"), h.get("degraded"))
        print("   %-12s %s  %5.2fs%s" % (p, st, dt, marker))
    except Exception as e:
        print("   %-12s FAIL %s" % (p, e))

print("\n=== transient/negative controls (token-gated route, expect 503/401) ===")
try:
    st, _, _ = req("/api/rankings/refresh", data=b"{}")
    print("   /api/rankings/refresh %s (no X-Admin-Token -> should be refused)" % st)
except urllib.error.HTTPError as e:
    print("   /api/rankings/refresh %s (refused, as expected)" % e.code)
except Exception as e:
    print("   %s" % e)

print("\nbefore:", polls())

print("\n=== POST /api/schedule/fetch?week=4 (the Schedule page's own call) ===")
try:
    st, body, dt = req("/api/schedule/fetch?week=4", data=b"{}")
    print("   status %s in %5.2fs, %s bytes" % (st, dt, len(body)))
except Exception as e:
    print("   FAIL", e)

time.sleep(5)
print("\nafter: ", polls())
n = d1_store.query("SELECT COUNT(*) AS n FROM weather_snapshots")
print("\ntotal rows now: %s" % (n[0]["n"] if n else "?"))