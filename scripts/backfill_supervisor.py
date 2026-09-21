#!/usr/bin/env python3
"""backfill_supervisor.py — keeps the D1 backfill moving to COMPLETION unattended.

Why: the worker stops politely when the 90K/day D1 write cap is hit (correct — the
free tier allows 100K/day). Without a supervisor that is where the job sits until a
human notices. This loop:

  * exits 0 once every expected chunk is done (or recorded no_data);
  * exits 1 if a chunk FAILED — a failed chunk means the counter caught something
    real and needs a human/agent, so it does not thrash;
  * relaunches the worker whenever no worker is alive (crash, kill, cap-stop);
  * WAITS OUT the daily cap: when the day's ledger is at the cap it sleeps until the
    next UTC midnight (the ledger's own day key rolls there — 17:00 PT under PDT),
    logging a heartbeat, then resumes from the checkpoint.

Launch detached:  nohup python -u scripts/backfill_supervisor.py >> logs/supervisor.log 2>&1 &
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CKPT = os.path.join(REPO, "data", "backfill_checkpoint.json")
LOCK = os.path.join(REPO, "data", "backfill.lock")
LOG = os.path.join(REPO, "logs", "backfill.log")
SEASONS = [2021, 2022, 2023, 2024, 2025, 2026]
JOBS = ["teams", "games", "lines", "ratings", "season_stats"]
EXPECTED = [f"{s}:{j}" for s in SEASONS for j in JOBS]
CAP = int(os.environ.get("D1_DAILY_WRITE_CAP", "90000"))
LEDGER = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                      "hermes", "d1_write_ledger.json")
POLL = 60
# A chunk writes in ~batch-sized steps and can need thousands of rows, so the
# supervisor must treat "within RESERVE of the cap" as capped — otherwise the
# worker bails out of assert_headroom, exits 0, and this loop relaunches it
# forever (observed 2026-09-21: ledger parked at 89,990/90,000, a relaunch every
# 5s for hours). The worker's own limit is `ledger + batch > cap`.
RESERVE = int(os.environ.get("D1_CAP_RESERVE", "2000"))
NO_PROGRESS_LIMIT = 3   # consecutive rc=0 worker runs that advance nothing


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}", flush=True)


def load_json(path: str, default: dict) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def ledger_state() -> tuple[str, int]:
    d = load_json(LEDGER, {"date": "", "rows_written": 0})
    return d.get("date", ""), int(d.get("rows_written", 0))


def lock_pid() -> int:
    try:
        return int(open(LOCK, encoding="utf-8").read().strip() or "0")
    except Exception:
        return 0


def pid_alive(pid: int) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                 capture_output=True, text=True, timeout=15).stdout
            return str(pid) in out
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def worker_alive() -> bool:
    pid = lock_pid()
    if pid and pid_alive(pid):
        return True
    return False


def sleep_until_next_utc_day(reason: str) -> None:
    now = datetime.now(timezone.utc)
    wake = (now + timedelta(days=1)).replace(hour=0, minute=2, second=0, microsecond=0)
    log(f"{reason}; sleeping until {wake.isoformat()} (~{(wake - now).total_seconds() / 3600:.1f}h) "
        f"then resuming from the checkpoint")
    while datetime.now(timezone.utc) < wake:
        time.sleep(min(POLL * 5, max(5, (wake - datetime.now(timezone.utc)).total_seconds())))
    log("new UTC day — ledger resets; resuming")


def launch_worker() -> int:
    log("launching worker: python -u scripts/backfill_d1.py")
    with open(LOG, "a", encoding="utf-8") as lf:
        lf.write(f"=== supervisor launch {datetime.now(timezone.utc).isoformat()} ===\n")
        lf.flush()
        p = subprocess.Popen([sys.executable, "-u", os.path.join(REPO, "scripts", "backfill_d1.py")],
                             cwd=REPO, stdout=lf, stderr=subprocess.STDOUT)
        return p.wait()


def main() -> int:
    log(f"supervisor up; {len(EXPECTED)} expected chunks")
    last_state = None
    no_progress = 0
    while True:
        ck = load_json(CKPT, {})
        done = set(ck.get("done", [])) | set(ck.get("no_data", []))
        missing = [t for t in EXPECTED if t not in done]
        failed = ck.get("failed") or {}

        if not missing:
            log(f"COMPLETE: all {len(EXPECTED)} chunks done/no_data "
                f"(rows_written={ck.get('rows_written')}, d1_today={ck.get('d1_ledger_written')})")
            return 0
        if failed:
            log(f"FAILED chunks {failed} — stopping for a human (worker said a write did not land)")
            return 1
        if worker_alive():
            state = (len(done), lock_pid())
            if state != last_state:
                log(f"worker alive (pid {lock_pid()}); {len(done)}/{len(EXPECTED)} chunks done, "
                    f"next={missing[0]}")
                last_state = state
            time.sleep(POLL)
            continue

        day, written = ledger_state()
        today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if day == today_utc and written >= CAP - RESERVE:
            sleep_until_next_utc_day(
                f"D1 daily write cap reached ({written}/{CAP}, reserve {RESERVE})")
            no_progress = 0
            continue

        log(f"no worker alive; {len(done)}/{len(EXPECTED)} done, resuming at {missing[0]} "
            f"(d1_today={written})")
        before = len(done)
        rc = launch_worker()
        after = len(load_json(CKPT, {}).get("done", []))
        log(f"worker exited rc={rc}; checkpoint={after} done")
        if rc == 3:
            # Worker reported the D1 daily write cap explicitly: wait for the reset
            # instead of relaunching into the same cap.
            sleep_until_next_utc_day(f"worker hit the D1 daily write cap "
                                     f"(d1_today={ledger_state()[1]})")
            no_progress = 0
            continue
        if rc == 0 and after <= before:
            no_progress += 1
            if no_progress >= NO_PROGRESS_LIMIT:
                # Worker keeps exiting cleanly without landing a chunk: the daily
                # write cap (or another blocker) — do not spin, wait for the reset.
                sleep_until_next_utc_day(
                    f"worker made no progress in {no_progress} runs "
                    f"({after} chunks done, d1_today={ledger_state()[1]})")
                no_progress = 0
                continue
        else:
            no_progress = 0
        if rc == 0 or rc == 1:
            time.sleep(5)          # rc=0: cap or complete; rc=1: failed -> handled next loop
        else:
            time.sleep(POLL)


if __name__ == "__main__":
    sys.exit(main())
