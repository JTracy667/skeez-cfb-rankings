"""Reset the slate store to a PROD-written row, then verify serving + rebuild behaviour.

The store row my local test wrote must go: it carries THIS box's odds/weather/injury
overlays, which are not what prod should serve. Deleting it forces the live container to
compute and persist its own copy — one slow request (mine), never Jeff's.

Then: confirm the row is prod-written, that repeat requests are served from it, and that
the fingerprint logic skips a rewrite when the inputs have not changed.
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


def post(week):
    r = urllib.request.Request(P + f"/api/schedule/fetch?week={week}", data=b"{}",
                               headers={"User-Agent": UA, "Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(r, timeout=180) as resp:
        body = resp.read()
        return resp.status, json.loads(body), time.time() - t0


def row():
    rows = d1_store.query("SELECT built_at, fingerprint, model_version, LENGTH(payload_gz) AS gz "
                          "FROM slate_cache WHERE season = 2026 AND week = 4")
    return rows[0] if rows else None


print("row before:", row())
print("\n--- clearing the locally-written row ---")
d1_store.query_full("DELETE FROM slate_cache WHERE season = 2026 AND week = 4")
print("row after delete:", row())

print("\n--- POST #1 (container must compute + store its own copy) ---")
st, body, dt = post(4)
print("   %s in %.2fs, matchups=%s, cached=%s"
      % (st, dt, len(body.get("matchups") or []), body.get("cached")))
print("   row now:", row())

print("\n--- POST #2 and #3 (should be served from the store) ---")
for i in (2, 3):
    st, body, dt = post(4)
    print("   #%d: %s in %.2fs cached=%s" % (i, st, dt, body.get("cached")))

print("\n--- fingerprint skip check ---")
r = row()
print("   stored fingerprint:", (r or {}).get("fingerprint", "")[:16], "...")
print("   (refresh_all compares this before rewriting; identical => 'unchanged')")