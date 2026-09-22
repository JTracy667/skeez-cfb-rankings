#!/usr/bin/env python3
"""probe_ratings_etag.py — learn whether CFBD's ratings ETag marks a real release.

THE QUESTION THIS ANSWERS
-------------------------
CFBD publishes ratings at an unpredictable time between Sunday night and Wednesday, and
we currently blind-poll the window. CFBD serves ETags on /ratings/{sp,elo,fpi}, and a
conditional GET with `If-None-Match` returns 304 + 0 bytes when unchanged. That would let
us probe often and pull the (expensive) full payload only when something actually changed.

UNVERIFIED, AND THE WHOLE POINT OF THIS PROBE: we know the mechanism works, but NOT that
the ETag flips when ratings change. Nobody can know that until a real release is observed.
This script observes one — cheaply, read-only, changing no serving path — and records it.

WHAT IT ALSO ANSWERS
  * whether the three endpoints (sp / elo / fpi) update together or independently, which
    decides whether one probe can stand in for three
  * whether the conditional-200 body is byte-identical to a plain GET, i.e. whether the
    ETag route still returns the SAME DATA (it should — same resource — but this proves it
    rather than assuming it)
  * real quota cost per probe, from CFBD's own X-CallLimit-Remaining header

DESIGN
  * Window-bounded: probes only Sun 18:00 -> Wed 23:59 PT. Outside it a release cannot
    happen, so it exits silently without spending a call.
  * SILENT when nothing changed (the cron no_agent contract: empty output == no message).
    Speaks only when an ETag actually flips, which is the news we want.
  * Read-only. Writes no D1 rows, touches no prod code path, does not affect the site.

Run:  python scripts/probe_ratings_etag.py            (normal, window-aware)
      python scripts/probe_ratings_etag.py --force    (ignore the window, probe now)
"""
import hashlib
import json
import os
import sys
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(HERE, "data", "ratings_etag_state.json")
HISTORY = os.path.join(HERE, "data", "ratings_etag_history.jsonl")
BASE = "https://api.collegefootballdata.com"
PT = ZoneInfo("America/Los_Angeles")
YEAR = 2026

ENDPOINTS = {                       # primary probe first; the rest are confirmed on change
    "sp": f"/ratings/sp?year={YEAR}",
    "elo": f"/ratings/elo?year={YEAR}",
    "fpi": f"/ratings/fpi?year={YEAR}",
}
PRIMARY = "sp"


def api_key():
    k = os.environ.get("CFBD_API_KEY")
    if k:
        return k
    for line in open(os.path.join(HERE, ".env"), encoding="utf-8"):
        if line.startswith("CFBD_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("no CFBD_API_KEY")


def in_release_window(now=None):
    """Sun 18:00 PT -> Wed 23:59 PT. Outside it, ratings cannot change."""
    now = now or datetime.now(PT)
    wd = now.weekday()               # Mon=0 .. Sun=6
    if wd in (0, 1, 2):              # Mon, Tue, Wed
        return True
    if wd == 6:                      # Sun, from 18:00 PT
        return now.hour >= 18
    return False


def get(path, etag=None, want_body=True):
    """GET (or conditional GET). Returns (status, etag, body_bytes, remaining)."""
    req = urllib.request.Request(BASE + path, method="GET")
    req.add_header("Authorization", "Bearer " + api_key())
    if etag:
        req.add_header("If-None-Match", etag)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read() if want_body else b""
            return r.status, r.headers.get("ETag"), body, r.headers.get("X-CallLimit-Remaining")
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return 304, e.headers.get("ETag"), b"", e.headers.get("X-CallLimit-Remaining")
        raise


def load_state():
    if os.path.exists(STATE):
        try:
            return json.load(open(STATE, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2, sort_keys=True)


def append_history(rec):
    os.makedirs(os.path.dirname(HISTORY), exist_ok=True)
    with open(HISTORY, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")


def main():
    force = "--force" in sys.argv
    st = load_state()
    etags = st.get("etags", {})

    # --- Baseline: no ETag recorded yet. Capture all three, and prove the conditional
    # path returns the same bytes as a plain GET (the "same data" question).
    if not etags:
        rec = {"ts": datetime.now(PT).isoformat(), "event": "baseline", "endpoints": {}}
        for name in ("sp", "elo", "fpi"):
            status, etag, body, rem = get(ENDPOINTS[name])
            rec["endpoints"][name] = {
                "status": status, "etag": etag, "bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest()[:16],
                "rows": len(json.loads(body)) if body else 0,
                "remaining": rem,
            }
            etags[name] = etag
        # equivalence check: forced-miss conditional GET must return the same body
        s1, _, b_plain, _ = get(ENDPOINTS[PRIMARY])
        s2, _, b_bogus, _ = get(ENDPOINTS[PRIMARY], etag='W/"definitely-not-the-etag"')
        rec["equivalence"] = {
            "plain_sha256": hashlib.sha256(b_plain).hexdigest()[:16],
            "bogus_etag_sha256": hashlib.sha256(b_bogus).hexdigest()[:16],
            "identical": b_plain == b_bogus,
            "plain_status": s1, "bogus_status": s2,
        }
        st["etags"] = etags
        st["baseline_ts"] = rec["ts"]
        save_state(st)
        append_history(rec)
        print("ETAG PROBE baseline recorded")
        for name, d in rec["endpoints"].items():
            print(f"  {name:4s} etag={d['etag']} rows={d['rows']} sha={d['sha256']} "
                  f"remaining={d['remaining']}")
        print(f"  conditional-200 returns identical bytes as plain GET: "
              f"{rec['equivalence']['identical']}")
        return 0

    if not force and not in_release_window():
        return 0                      # silent: no release can happen outside the window

    # --- Probe: conditional GET on the primary endpoint only (1 call).
    status, etag, body, rem = get(ENDPOINTS[PRIMARY], etag=etags.get(PRIMARY))
    if status == 304:
        return 0                      # silent: nothing changed

    # --- CHANGE DETECTED. Confirm the others, and record everything.
    rec = {
        "ts": datetime.now(PT).isoformat(), "event": "change_detected",
        "primary": PRIMARY, "old_etag": etags.get(PRIMARY), "new_etag": etag,
        "bytes": len(body), "rows": len(json.loads(body)) if body else 0,
        "sha256": hashlib.sha256(body).hexdigest()[:16], "remaining": rem,
        "others_changed": {},
    }
    for name in ("elo", "fpi"):
        s, e, b, _ = get(ENDPOINTS[name], etag=etags.get(name))
        rec["others_changed"][name] = (s != 304)
        rec[f"{name}_rows"] = len(json.loads(b)) if b else 0
        if s != 304 and e:
            etags[name] = e
    etags[PRIMARY] = etag
    st["etags"] = etags
    save_state(st)
    append_history(rec)

    others = ", ".join(f"{k}={'changed' if v else 'unchanged'}"
                       for k, v in rec["others_changed"].items())
    print("ETAG PROBE: RATINGS RELEASE DETECTED")
    print(f"  {PRIMARY}: etag changed, rows={rec['rows']} sha={rec['sha256']}")
    print(f"  {others}")
    print("  -> the ETag DOES flip on a release; conditional polling is viable")
    return 0


if __name__ == "__main__":
    sys.exit(main())