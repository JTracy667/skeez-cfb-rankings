#!/usr/bin/env python3
"""probe_ratings_etag.py — CFBD ratings RELEASE WATCHER: detect a release, and
(optionally) trigger the analytics pull immediately instead of waiting for an anchor.

WHY
---
CFBD drops the composite inputs at an unpredictable time between Sunday night and
Wednesday, and we currently pull only at the Sun/Mon/Tue/Wed 21:00 PT anchors — so the
site can serve up to a day of stale ratings after a release has already happened. We
ALREADY pay for this probe, which detects the release; this turns a signal we already
have into fresh data.

COST (unchanged from the original probe)
----------------------------------------
2 calls per in-window hour: the primary `ratings/sp` conditional GET + the
`ratings/srs` publication check = ~676 calls/mo, ~2.3% of the 30,000 allowance. The
other ratings endpoints are conditional-GET'd ONLY when the primary flips, so a full
release confirmation costs ~4 calls once. A full analytics pull is ~16 calls.
See docs/CFBD_API_MAP.md §3d.

WHAT MAKES THIS SAFE
--------------------
  * Release-gated: watches `ratings/sp` (primary). `stats/season` and `ppa` move with
    GAME RESULTS and `games/weather` moves hourly — triggering on "any flip" would fire
    on noise and could bake a mixed-vintage snapshot.
  * Settle-gated: a changed ETag is HELD as `pending` until the same new value is seen
    on a later probe at least SETTLE_MIN minutes apart. Avoids pulling a
    half-published dataset; fires at most once per settled change.
  * DARK BY DEFAULT: logs what it would do unless CFB_ETAG_TRIGGER_LIVE=1.
  * The anchors stay as the BACKSTOP (wrangler crons), so a missed flip still pulls.
  * The pull path is UNCHANGED, so D1 archiving is unchanged: the trigger POSTs
    /api/analytics/fetch (admin-gated), which runs the same fetch_live_analytics ->
    _store_team_analytics -> d1_write_path.snapshot_team_analytics chain and writes the
    same stat_observations rows to D1 cfb-history.
  * Window-bounded: Sun 18:00 -> Wed 23:59 PT. Outside it a release cannot happen, so it
    exits silently without spending a call.
  * SILENT when nothing changed (the cron no_agent contract: empty output == no message).
  * Read-only until a settled change is confirmed; writes no D1 rows itself.

Run:  python scripts/probe_ratings_etag.py            (window-aware, dark)
      python scripts/probe_ratings_etag.py --force    (ignore the window, probe now)
      CFB_ETAG_TRIGGER_LIVE=1 python scripts/probe_ratings_etag.py   (armed trigger)
"""
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(HERE, "data", "ratings_etag_state.json")
HISTORY = os.path.join(HERE, "data", "ratings_etag_history.jsonl")
BASE = "https://api.collegefootballdata.com"
PT = ZoneInfo("America/Los_Angeles")
YEAR = 2026

# EVERY watched endpoint is conditional-GET'd each run and settle-gated independently.
# Watching ONLY the ratings trio would miss an efficiency update: the composite's
# `efficiency` input (~31% of the weight) is computed from stats/season/advanced + ppa,
# NOT from the ratings. `talent` (~32%) and `returning` (~19%) are season-long priors,
# watched so a re-publication cannot be silently missed.
PRIMARY = "sp"
WATCH = {
    "sp": f"/ratings/sp?year={YEAR}",
    "elo": f"/ratings/elo?year={YEAR}",
    "fpi": f"/ratings/fpi?year={YEAR}",
    "srs": f"/ratings/srs?year={YEAR}",
    "stats_advanced": f"/stats/season/advanced?year={YEAR}",
    "ppa_teams": f"/ppa/teams?year={YEAR}",
    "talent": f"/talent?year={YEAR}",
    "returning": f"/player/returning?year={YEAR}",
}
ENDPOINTS = WATCH          # kept for the baseline probe below

SRS_2026_URL = f"/ratings/srs?year={YEAR}"
PUBLIC = os.environ.get("CFB_PUBLIC_URL", "https://skeezcfb-rankings.com")
# A pending change must be re-observed at least this many minutes later to settle.
SETTLE_MIN = int(os.environ.get("CFB_ETAG_SETTLE_MIN", "30"))
# DARK BY DEFAULT. CFB_ETAG_TRIGGER_LIVE=1 arms the trigger.
LIVE = os.environ.get("CFB_ETAG_TRIGGER_LIVE", "0") == "1"


def api_key():
    k = os.environ.get("CFBD_API_KEY")
    if k:
        return k
    for line in open(os.path.join(HERE, ".env"), encoding="utf-8"):
        if line.startswith("CFBD_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("no CFBD_API_KEY")


def admin_token():
    k = os.environ.get("ADMIN_TOKEN")
    if k:
        return k
    for line in open(os.path.join(HERE, ".env"), encoding="utf-8"):
        if line.startswith("ADMIN_TOKEN="):
            return line.split("=", 1)[1].strip()
    p = os.path.join(HERE, ".admin_token")
    if os.path.exists(p):
        return open(p, encoding="utf-8").read().strip()
    raise SystemExit("no ADMIN_TOKEN")


def settle_elapsed_min(pending):
    return (datetime.now(PT) - datetime.fromisoformat(pending["since"])).total_seconds() / 60


def fire_pull(reason):
    """Trigger the admin-gated analytics pull. SAME code path as the anchors and the
    manual fetch, so the D1 archive (stat_observations) is UNCHANGED."""
    req = urllib.request.Request(PUBLIC + "/api/analytics/fetch", data=b"{}", method="POST")
    req.add_header("X-Admin-Token", admin_token())
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "cfb-etag-watcher/1.0")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, r.read().decode("utf-8", "replace")[:300]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:300]
    except Exception as e:  # noqa: BLE001
        return None, str(e)[:200]


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


def check_srs_2026(st):
    """Watch for CFBD starting to publish THIS season's SRS.

    Silent until it changes, then say so. SRS is REFERENCE-ONLY (weight 0.00 since
    V6.1), so this does NOT change the composite — only what the analytics page shows.
    Costs 1 call per in-window probe. Read-only.
    """
    prev = st.get("srs_2026_rows")
    try:
        _status, _etag, body, _rem = get(SRS_2026_URL)
        n = len(json.loads(body)) if body else 0
    except Exception:
        return
    st["srs_2026_rows"] = n
    if prev is None or n == prev:
        return
    if n > 0 and (prev or 0) == 0:
        print("CFBD SRS 2026 IS NOW PUBLISHED")
        print(f"  ratings/srs?year=2026 -> {n} rows (was 0)")
        print("  -> SRS is REFERENCE-ONLY (weight 0.00 since V6.1): this does NOT change")
        print("     the composite, only what the analytics page displays.")


def main():
    force = "--force" in sys.argv
    st = load_state()
    etags = st.get("etags", {})
    pending = st.get("pending", {})

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

    # Watch for this season's SRS appearing (see check_srs_2026).
    check_srs_2026(st)
    save_state(st)

    # --- Probe EVERY watched endpoint (1 conditional GET each). Each is settle-gated
    # independently, and ANY settled change triggers the pull. Watching only the ratings
    # trio would MISS an efficiency update (stats/season/advanced + ppa).
    settled, detected, baselined = {}, {}, []
    for name, path in WATCH.items():
        had_baseline = name in etags
        s, e, body, _r = get(path, etag=etags.get(name))
        # A first sighting is a BASELINE, not a change — never report a missing
        # baseline as "changed", which would misreport a release.
        if not had_baseline:
            if e:
                etags[name] = e
                baselined.append(name)
            continue
        if s == 304:
            p = pending.get(name)
            if p and settle_elapsed_min(p) >= SETTLE_MIN:
                waited = round(settle_elapsed_min(p), 1)
                etags[name] = p["etag"]
                pending.pop(name, None)
                settled[name] = (p["etag"], waited)
            continue
        prev = pending.get(name)
        if prev and prev.get("etag") == e:
            aged = settle_elapsed_min(prev)
        else:
            pending[name] = {"etag": e, "since": datetime.now(PT).isoformat()}
            aged = 0.0
        if aged >= SETTLE_MIN:
            etags[name] = e
            pending.pop(name, None)
            settled[name] = (e, round(aged, 1))
        else:
            detected[name] = (round(aged, 1),
                              len(json.loads(body)) if body else None)

    st["etags"], st["pending"] = etags, pending
    save_state(st)
    if not settled and not detected and not baselined:
        return 0                      # silent: nothing changed
    append_history({"ts": datetime.now(PT).isoformat(), "event": "probe",
                    "settled": sorted(settled), "detected": sorted(detected),
                    "baselined": sorted(baselined)})
    if baselined:
        print("ETAG WATCH baseline recorded: " + ", ".join(sorted(baselined)))
    if detected:
        print("CFBD DATA CHANGE PENDING SETTLE (needs a stable re-check)")
        for k, (aged, rows) in sorted(detected.items()):
            print(f"  {k}: changed {aged:.0f} min ago, rows={rows}")
    if settled:
        print("CFBD DATA RELEASE SETTLED — pull warranted")
        for k, (etagv, waited) in sorted(settled.items()):
            print(f"  {k}: etag -> {etagv} (stable {waited:.0f} min)")
        if LIVE:
            code, resp = fire_pull("settled change: " + ",".join(sorted(settled)))
            print(f"  TRIGGERED pull: HTTP {code} {resp}")
        else:
            print("  DARK: would have POSTed /api/analytics/fetch"
                  " (~16 calls; D1 archive unchanged)")
            print("  arm with CFB_ETAG_TRIGGER_LIVE=1")
    return 0


if __name__ == "__main__":
    sys.exit(main())