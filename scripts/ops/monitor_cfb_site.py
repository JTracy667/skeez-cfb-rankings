#!/usr/bin/env python3
"""Watchdog for skeezcfb-rankings.com (Cloudflare Containers).

Design rules
  * SILENT when healthy — empty stdout means the cron delivers nothing. Only
    failures, throttled repeats, and recoveries print.
  * Exercises cold starts on purpose: at a 30-minute cadence the container's
    20-minute sleepAfter elapses between checks, so each run is usually a cold
    start — exactly the failure mode (bad cold start / OOM) we must detect.
  * Guards the regression classes we have actually hit:
      - degraded rankings after a refresh (Elo null / SP+ 0.0)
      - rankings not sorted by composite
      - a scheduled CFBD analytics anchor that never landed (due=true too long)
  * State lives on disk so repeats can be throttled without an LLM.
  * (Render was retired Sep 2026 — alerts no longer probe or mention it.)

Usage:  monitor_cfb_site.py [--simulate-fail] [--status]
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

SITE = os.environ.get("CFB_MONITOR_URL", "https://skeezcfb-rankings.com")
STATE_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                         "hermes", "profiles", "cto", "state")
STATE = os.path.join(STATE_DIR, "cfb_site_monitor.json")
HDRS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0 Safari/537.36",
        "Accept": "application/json,*/*", "Accept-Language": "en-US,en;q=0.9"}
PAGES = ["/", "/analytics", "/schedule", "/win-totals"]
SLOW_MS = 15000          # cold start should be well under a second; 15s = sick
ANCHOR_GRACE_H = 3       # hours after a 9pm PT anchor before a missing pull is a fault
SETTLE_SECONDS = int(os.environ.get("CFB_SETTLE_SECONDS", "150"))  # re-check window (see below)
REPEAT_EVERY = 2         # at 30m cadence: re-alert every 2nd failure (= hourly)

if "--simulate-fail" in sys.argv:
    SITE = "https://this-host-does-not-exist.invalid"


def get(url, timeout=60):
    t0 = time.time()
    req = urllib.request.Request(url, headers=HDRS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), (time.time() - t0) * 1000
    except urllib.error.HTTPError as e:
        return e.code, e.read(), (time.time() - t0) * 1000
    except Exception as e:
        return None, f"{type(e).__name__}: {e}".encode(), (time.time() - t0) * 1000


def post_json(url, timeout=60):
    req = urllib.request.Request(url, data=b"{}", method="POST",
                                 headers={**HDRS, "Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), (time.time() - t0) * 1000
    except urllib.error.HTTPError as e:
        return e.code, e.read(), (time.time() - t0) * 1000
    except Exception as e:
        return None, f"{type(e).__name__}: {e}".encode(), (time.time() - t0) * 1000


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(s):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(STATE, "w") as f:
            json.dump(s, f, indent=2)
    except Exception:
        pass


fails, details, slow = [], [], []

for p in PAGES:
    st, body, ms = get(SITE + p)
    if st != 200:
        fails.append(f"{p} -> {st if st else 'unreachable'}")
    elif ms > SLOW_MS:
        slow.append(f"{p} {ms/1000:.1f}s")

st, body, ms = get(SITE + "/api/health")
health_ok = False
build_stamp = None
if st == 200:
    try:
        h = json.loads(body)
        health_ok = h.get("status") == "ok"
        build_stamp = h.get("build")
        if not health_ok:
            fails.append(f"/api/health body: {body[:80]!r}")
    except Exception:
        fails.append(f"/api/health unparsable: {body[:80]!r}")
else:
    fails.append(f"/api/health -> {st if st else 'unreachable'}")

# Record the live build stamp on every run.
#
# Why this belongs here: a Cloudflare container keeps serving the PREVIOUS image
# until it has been idle for sleepAfter (20m), so "has my deploy actually gone
# live?" is a recurring question -- and the usual way to answer it (curl the
# health endpoint) resets the idle timer and DELAYS the very swap being waited
# for. This watchdog already probes /api/health on a 30-minute cadence, which is
# deliberately longer than sleepAfter, so it is the one probe that can observe the
# cold start without breaking it. Writing the stamp to a file turns that existing
# probe into a deploy receipt at zero extra requests.
#
# Stays silent on stdout to honour the no_agent contract (failures only).
try:
    _stamp_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "cache")
    os.makedirs(_stamp_dir, exist_ok=True)
    with open(os.path.join(_stamp_dir, "cfb_build_stamp.txt"), "w", encoding="utf-8") as f:
        f.write("%s build=%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), build_stamp))
except Exception:
    pass

# rankings: count, sort order, and the degraded-refresh signature
st, body, ms = get(SITE + "/api/rankings")
if st == 200:
    try:
        teams = json.loads(body)["teams"]
        if len(teams) != 25:
            fails.append(f"/api/rankings returned {len(teams)} teams (expected 25)")
        if not all(teams[i]["composite"] >= teams[i + 1]["composite"] for i in range(len(teams) - 1)):
            fails.append("/api/rankings NOT sorted by composite")
        null_elo = sum(1 for t in teams if t.get("elo") in (None, 0))
        zero_sp = sum(1 for t in teams if not t.get("sp_plus"))
        if null_elo or zero_sp:
            fails.append(f"/api/rankings degraded: {null_elo} null Elo, {zero_sp} zero SP+")
    except Exception as e:
        fails.append(f"/api/rankings unparsable: {e}")
else:
    fails.append(f"/api/rankings -> {st if st else 'unreachable'}")

st, body, _ = get(SITE + "/api/win-totals")
if st == 200:
    try:
        n = len(json.loads(body).get("teams", []))
        if n < 100:
            fails.append(f"/api/win-totals only {n} teams")
    except Exception:
        fails.append("/api/win-totals unparsable")
else:
    fails.append(f"/api/win-totals -> {st if st else 'unreachable'}")

st, body, _ = get(SITE + "/api/schedule/current-week")
if st == 200:
    try:
        wk = json.loads(body).get("week")
        if not wk or wk < 1:
            fails.append(f"/api/schedule/current-week returned week {wk!r}")
    except Exception:
        fails.append("/api/schedule/current-week unparsable")
else:
    fails.append(f"/api/schedule/current-week -> {st if st else 'unreachable'}")

# Invariant: ops endpoints must stay closed to anonymous callers. A gate that
# silently disappears is invisible in normal use, so assert it every run. The
# POST is rejected before any work happens, so it costs no API quota.
st, _, _ = post_json(SITE + "/api/analytics/fetch")
if st != 401:
    fails.append(f"/api/analytics/fetch answered {st} without a token (expected 401) — ops gate may be down")

# freshness: a scheduled 9pm PT anchor that never produced a pull is a fault
st, body, _ = get(SITE + "/api/analytics/pull-status")
if st == 200:
    try:
        d = json.loads(body)
        anchor = d.get("most_recent_anchor_utc")
        if d.get("due") and anchor:
            age_h = (datetime.now(timezone.utc)
                     - datetime.fromisoformat(anchor.replace("Z", "+00:00"))).total_seconds() / 3600
            if age_h > ANCHOR_GRACE_H:
                # SETTLE GRACE — a container start repairs stale data within ~2 minutes
                # (the daemon pulls ~10s after boot), so a probe landing in that window
                # sees a genuinely-stale state that is ALREADY being fixed. Observed
                # 2026-09-22: alerted on "last pull never" and self-healed 90s later.
                # Re-check once before calling it a fault; a real outage still alerts,
                # just SETTLE_SECONDS later.
                time.sleep(SETTLE_SECONDS)
                _, body2, _ = get(SITE + "/api/analytics/pull-status")
                try:
                    d2 = json.loads(body2) if body2 else {}
                except Exception:
                    d2 = {}
                if d2.get("due"):
                    a2 = d2.get("most_recent_anchor_utc") or anchor
                    age_h2 = (datetime.now(timezone.utc)
                              - datetime.fromisoformat(a2.replace("Z", "+00:00"))
                              ).total_seconds() / 3600
                    last = d2.get("last_pull_utc") or "never"
                    fails.append(f"analytics refresh overdue: anchor {anchor} is {age_h2:.1f}h old, "
                                 f"last pull {last}, due=True "
                                 f"(re-confirmed after {SETTLE_SECONDS}s — not a startup transient)")
                else:
                    # Self-repaired during the settle. Must stay SILENT on stdout (the
                    # no_agent contract: empty output == nothing delivered), so log it
                    # to a file instead.
                    try:
                        with open(os.path.join(_stamp_dir, "cfb_watchdog_transients.log"),
                                  "a", encoding="utf-8") as f:
                            f.write("%s staleness was due on first probe (last pull %s) "
                                    "but repaired within %ss\n"
                                    % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                       d.get("last_pull_utc") or "never", SETTLE_SECONDS))
                    except Exception:
                        pass
    except Exception:
        fails.append("/api/analytics/pull-status unparsable")
else:
    fails.append(f"/api/analytics/pull-status -> {st if st else 'unreachable'}")

# ---- Phase 3.5: standing budget meters (80% alert / 95% pause) -------------
# Deliberately NOT appended to `fails`: the site is serving fine, it's the QUOTA
# that's the issue, and an 85% CFBD burn must never masquerade as "SITE FAILING"
# — that would train the reader to ignore real outages. Separate list, separate
# state key, alerts on level CHANGES so it can't spam every 30 minutes.
warns = []
st, body, _ = get(SITE + "/api/budget")
if st == 200:
    try:
        b = json.loads(body)
        for src, v in (b.get("sources") or {}).items():
            lvl = v.get("level")
            if lvl == "pause":
                warns.append(f"{src} PAUSED at {v['pct']}% of {v['period']} cap "
                             f"({v['used']}/{v['limit']}) — calls stopped, serving stale data")
            elif lvl == "alert":
                warns.append(f"{src} at {v['pct']}% of {v['period']} cap "
                             f"({v['used']}/{v['limit']}) — tier-upgrade decision due (Jeff policy)")
    except Exception as e:
        warns.append(f"/api/budget unparsable: {e}")
elif st and st >= 500:
    warns.append(f"/api/budget -> {st}")
# 404 == image predates the meters; not a fault, stay quiet.

now = datetime.now(timezone.utc)
state = load_state()
prev_state = state.get("state", "ok")
consec = int(state.get("consecutive_fail", 0))

if fails:
    consec += 1
    lines = [f"⚠️ CFB SITE WATCHDOG — {SITE} FAILING ({consec} consecutive check{'s' if consec > 1 else ''})",
             f"at {now.strftime('%Y-%m-%d %H:%MZ')} ({now.astimezone().strftime('%H:%M %Z')})"]
    for f_ in fails[:8]:
        lines.append(f"  • {f_}")
    if slow:
        lines.append(f"  • slow: {', '.join(slow)}")
    lines.append("  • runbook: check `wrangler containers list`, then the Worker cron "
                 "`0 4,5 * * 1,2,3,4`; rollback = DNS snapshot in %TEMP%\\cfb_dns_rollback.json")
    if consec == 1:
        print("\n".join(lines))
    elif consec % REPEAT_EVERY == 0:
        print(f"⚠️ CFB SITE WATCHDOG still failing ({consec} consecutive). "
              f"First fault: {', '.join(fails[:3])}")
    # else: throttled — stay silent
    state["state"] = "failing"
else:
    if prev_state == "failing":
        print(f"✅ CFB SITE WATCHDOG recovered — {SITE} healthy again at "
              f"{now.strftime('%Y-%m-%d %H:%MZ')} (was failing {consec} checks)")
    consec = 0
    state["state"] = "ok"
    if slow and state.get("slow_reported") != now.strftime("%Y-%m-%d"):
        print(f"ℹ️ CFB SITE WATCHDOG: healthy but slow — {', '.join(slow)}")
        state["slow_reported"] = now.strftime("%Y-%m-%d")

# ---- budget warnings: alert on level CHANGE only (no 30-min spam) ----------
_sig = "|".join(sorted(warns))
if warns and _sig != state.get("budget_warn_sig"):
    print("⚠️ CFB BUDGET — API quota warning (site is serving; this is a CAP issue, not an outage)")
    for w in warns[:8]:
        print(f"  • {w}")
    print("  • policy (Jeff): approaching 80% = tier-upgrade decision, not throttling; "
          "95% = calls stopped automatically and data served stale-but-honest")
elif not warns and state.get("budget_warn_sig"):
    print("✅ CFB BUDGET — all API quotas back under the 80% alert line")
state["budget_warn_sig"] = _sig

state["consecutive_fail"] = consec
state["last_check_utc"] = now.isoformat()
state["last_result"] = "fail" if fails else "ok"
save_state(state)

if "--status" in sys.argv:
    print(json.dumps(state, indent=2))