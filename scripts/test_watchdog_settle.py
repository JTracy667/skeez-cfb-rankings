#!/usr/bin/env python3
"""test_watchdog_settle.py — verify the watchdog's SETTLE GRACE, without touching prod.

Serves a local stub of skeezcfb-rankings.com and runs a COPY of the site monitor
against it. The copy matters: the monitor derives its build-stamp and state paths from
__file__ and LOCALAPPDATA, so running a copy inside a temp tree leaves the real cache
and state files untouched.

The two scenarios are the two honest outcomes of the grace window:

  A. due=True on the first probe, due=False after the settle
     -> MUST stay SILENT on stdout (the no_agent contract: empty output == nothing is
        delivered) and log a transient line instead.
  B. due=True on the first probe AND after the settle
     -> MUST still report the fault on stdout. A grace period that swallows genuine
        faults is worse than the false alarm it fixes, so this is the one that matters.

Why this lives in the repo: it was originally left in a profile cache directory, which
is pruned after 72h — so the verifier could not find the artifact it was asked to run.
A test someone must run belongs in the repo, next to the code, not in a scratch cache.

The monitor itself runs from the CTO profile (it is a cron deliverable, not app code).
This test therefore checks that the copy it exercises MATCHES the copy that actually
runs in production, and says so loudly if they have drifted.

Run:  python scripts/test_watchdog_settle.py
Env:  CFB_MONITOR_SRC   override the monitor path under test
"""
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_COPY = os.path.join(HERE, "ops", "monitor_cfb_site.py")
PROD_COPY = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    "hermes", "profiles", "cto", "scripts", "monitor_cfb_site.py")

STATE = {"scenario": "A", "pull_status_calls": 0}


def resolve_monitor():
    """Path of the monitor to exercise, plus a drift note against the prod copy."""
    src = os.environ.get("CFB_MONITOR_SRC")
    if not src:
        src = REPO_COPY if os.path.exists(REPO_COPY) else PROD_COPY
    drift = None
    if os.path.exists(src) and os.path.exists(PROD_COPY):
        try:
            a = open(src, "rb").read()
            b = open(PROD_COPY, "rb").read()
            if a != b:
                drift = f"{os.path.basename(src)} differs from the production cron copy"
        except Exception as e:
            drift = f"could not compare: {e}"
    return src, drift


class Stub(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        if self.path.startswith("/api/analytics/fetch"):
            self._send(401, '{"detail":"missing X-Admin-Token"}')   # ops gate UP
        else:
            self._send(200, "{}")

    def do_GET(self):
        p = self.path.split("?")[0]
        old_anchor = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
        if p == "/api/health":
            self._send(200, json.dumps({"status": "ok", "build": "stub", "teams": 25,
                                        "degraded": False, "degraded_sources": [],
                                        "cache_ttl": 300, "budget": {}}))
        elif p == "/api/analytics/pull-status":
            STATE["pull_status_calls"] += 1
            first = STATE["pull_status_calls"] == 1
            due = True if (first or STATE["scenario"] == "B") else False
            self._send(200, json.dumps({
                "last_pull_utc": None,
                "last_pull_age_hours": None,
                "due": due,
                "most_recent_anchor_utc": old_anchor,
                "schedulers": ["Sun 21:00 PT"],
            }))
        elif p == "/api/rankings":
            teams = [{"team_id": i, "name": f"T{i}", "composite": 100 - i, "rank": i + 1,
                      "elo": 1500 + i, "sp_plus": 10.0 + i} for i in range(25)]
            self._send(200, json.dumps({"teams": teams}))
        elif p == "/api/win-totals":
            # the monitor requires >= 100 rows
            self._send(200, json.dumps({"teams": [{"team_id": i, "name": f"T{i}",
                                                   "composite": 70.0 - (i / 10.0)}
                                                  for i in range(130)]}))
        elif p == "/api/schedule/current-week":
            self._send(200, json.dumps({"week": 4, "season": 2026}))
        elif p == "/api/budget":
            self._send(200, json.dumps({"sources": {}}))
        elif p in ("/", "/analytics", "/schedule", "/win-totals"):
            self._send(200, "<html><body>stub</body></html>", "text/html")
        else:
            self._send(200, "{}")


def run_scenario(src, scenario):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    port = srv.server_address[1]
    STATE["scenario"] = scenario
    STATE["pull_status_calls"] = 0
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    tmp = tempfile.mkdtemp(prefix="wdtest_")
    os.makedirs(os.path.join(tmp, "scripts"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "cache"), exist_ok=True)
    copy = os.path.join(tmp, "scripts", "monitor_cfb_site.py")
    shutil.copyfile(src, copy)

    env = dict(os.environ)
    env["CFB_MONITOR_URL"] = f"http://127.0.0.1:{port}"
    env["CFB_SETTLE_SECONDS"] = "3"
    env["LOCALAPPDATA"] = tmp          # redirects the monitor's STATE file
    try:
        r = subprocess.run([sys.executable, copy, "--once"], capture_output=True,
                           text=True, timeout=120, env=env)
    except subprocess.TimeoutExpired:
        r = None
    srv.shutdown()

    log = os.path.join(tmp, "cache", "cfb_watchdog_transients.log")
    logged = os.path.exists(log) and "repaired within" in open(log, encoding="utf-8").read()
    out = (r.stdout or "") if r else "<timeout>"
    rc = r.returncode if r else -1
    shutil.rmtree(tmp, ignore_errors=True)
    return out, rc, logged, STATE["pull_status_calls"]


def main():
    src, drift = resolve_monitor()
    print(f"monitor under test : {src}")
    print(f"  exists           : {os.path.exists(src)}")
    if drift:
        print(f"  !!! DRIFT        : {drift} — the copy under test is NOT what prod runs")
    else:
        print("  vs prod cron copy: identical (or prod copy unavailable)")
    if not os.path.exists(src):
        print("\nRESULT: cannot run — monitor not found. Set CFB_MONITOR_SRC.")
        return 2
    print()

    print("=== scenario A: due on first probe, repaired during the settle ===")
    outA, rcA, loggedA, callsA = run_scenario(src, "A")
    print(f"  pull-status calls : {callsA} (expect >=2: the re-check happened)")
    print(f"  stdout silent     : {outA.strip() == ''!r}  rc={rcA}")
    print(f"  transient logged  : {loggedA}")
    if outA.strip():
        print("  --- stdout was NOT empty; contents: ---")
        for line in outA.strip().splitlines()[:12]:
            print("   |", line)
    a_ok = (outA.strip() == "" and loggedA and callsA >= 2)
    print(f"  -> {'PASS' if a_ok else 'FAIL'}\n")

    print("=== scenario B: still due after the settle (a real fault) ===")
    outB, rcB, loggedB, callsB = run_scenario(src, "B")
    reported = "refresh overdue" in outB
    print(f"  stdout reported   : {reported!r}  rc={rcB}")
    print(f"  excerpt           : {outB.strip().splitlines()[0][:110] if outB.strip() else '(empty)'}")
    b_ok = reported
    print(f"  -> {'PASS' if b_ok else 'FAIL'}\n")

    print(f"RESULT: {'2/2 PASS' if (a_ok and b_ok) else 'FAILURES PRESENT'}")
    return 0 if (a_ok and b_ok) else 1


if __name__ == "__main__":
    sys.exit(main())