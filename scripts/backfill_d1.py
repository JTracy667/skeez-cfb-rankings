#!/usr/bin/env python3
"""backfill_d1.py — paced 5-season backfill into D1 `cfb-history` (spec §4).

Scope (spec §7.2): 2021-2025 full seasons + 2026 weeks 1-3. TEAM-LEVEL ONLY
(players deferred per spec §7.1).

FIXES over v1 (2026-09-21, both found by post-run verification):
  * CFBD ratings/stats endpoints key by team NAME, not teamId -> build a
    name->team_id map from /teams and REQUIRE a match. Unmatched names are
    counted and reported, never silently written with a NULL id.
  * A chunk is only marked done if it CONFIRMED writes -> no more "season_stats
    done, 0 rows written".
  * Single-runner LOCK file -> two backfills cannot race the checkpoint.
  * cfbd_calls counted in memory; checkpoint saved once per chunk.

COUNTER (CEO directive 2026-09-21): `rows_written` is the sum of rows the D1 API
confirmed it wrote (`d1_store.confirmed_writes` off the response meta). There is
no local fallback. A chunk that fetched data but confirmed ZERO writes FAILS —
it is recorded under `failed` in the checkpoint, is NOT marked done, and the run
exits non-zero. Only a chunk whose CFBD source returned nothing at all is
recorded as `no_data` (nothing to write) and skipped without failing the run.

RUNNING: takes a PID-aware lock, so a killed run's stale lock is stolen instead
of blocking the resume. Intended to be launched DETACHED (background) so a turn
timeout can never kill it:  nohup python -u run_all.py > logs/backfill.log 2>&1 &

Guarantees: checkpoint per (season, endpoint); resume never restart; idempotent;
D1 90K row-writes/day guard; CFBD + D1 budget counters logged per chunk.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import d1_store  # noqa: E402
import cfbd_shared  # noqa: E402 — the SHARED CFBD client (also used by app.py)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CKPT = os.path.join(REPO, "data", "backfill_checkpoint.json")
LOCK = os.path.join(REPO, "data", "backfill.lock")
SEASONS = [2021, 2022, 2023, 2024, 2025, 2026]
THROTTLE = 0.30
MAX_WEEK = 16

CALLS = 0           # CFBD requests this run
SRC_ROWS = 0        # rows the CURRENT chunk's CFBD calls returned
ROWS_WRITTEN = 0    # rows D1 CONFIRMED writing this run
NAME2ID: dict[str, int] = {}
UNMATCHED: list[str] = []


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def cfbd(path: str, params: dict | None = None):
    """Thin adapter over the SHARED CFBD client (cfbd_shared.cfbd_get) — the same
    client the live app uses. `params` map onto its extra query kwargs."""
    global CALLS, SRC_ROWS
    p = dict(params or {})
    year = p.pop("year", SEASONS[-1])
    CALLS += 1
    time.sleep(THROTTLE)          # backfill-specific pacing (the app polls hourly)
    data = cfbd_shared.cfbd_get(path, year=year, **p) or []
    SRC_ROWS += len(data)
    return data


# ------------------------------------------------------------------ checkpoint
def _load() -> dict:
    try:
        with open(CKPT, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"done": [], "cfbd_calls": 0, "started": None}


def _save(c: dict) -> None:
    os.makedirs(os.path.dirname(CKPT), exist_ok=True)
    c["done"] = sorted(set(c.get("done", [])))
    c["cfbd_calls"] = CALLS
    c["rows_written"] = ROWS_WRITTEN            # CONFIRMED by the D1 API, this run
    c["d1_ledger_written"] = d1_store.ledger_written()   # day total, D1-confirmed
    if not c.get("started"):
        c["started"] = _ts()
    with open(CKPT, "w", encoding="utf-8") as f:
        json.dump(c, f, indent=2)


def _tid(name: str | None):
    """Map a CFBD team NAME -> stable team_id. None if unknown -> caller skips."""
    if not name or name == "nationalAverages":
        return None
    tid = NAME2ID.get(name)
    if tid is None and name not in UNMATCHED:
        UNMATCHED.append(name)
    return tid


def load_name_map(season: int) -> None:
    """REUSE the live app's team matcher (cfbd_shared.teams_by_name -> {school: team}).
    Loaded independently of the teams chunk so a resumed run still maps names."""
    for school, t in (cfbd_shared.teams_by_name() or {}).items():
        if t.get("id"):
            NAME2ID.setdefault(school, t["id"])
    print(f"  name->id map loaded from cfbd_shared.teams_by_name(): {len(NAME2ID)} teams",
          flush=True)


# ------------------------------------------------------------------- workers
def seed_teams(season: int) -> int:
    rows = cfbd("teams", {"year": season})
    out = []
    for t in rows:
        if not t.get("id"):
            continue
        out.append({"team_id": t["id"], "name": t.get("school"), "abbr": t.get("abbreviation"),
                    "conference": t.get("conference"), "first_season": season})
    return d1_store.upsert_teams(out)


def do_games(season: int) -> int:
    data = cfbd("games", {"year": season})
    rows = [{"game_id": g["id"], "season": g.get("season"), "week": g.get("week"),
             "home_id": g.get("homeId"), "away_id": g.get("awayId"),
             "kickoff": g.get("startDate"), "home_score": g.get("homePoints"),
             "away_score": g.get("awayPoints"),
             "status": "final" if g.get("completed") else "scheduled",
             "venue": g.get("venue"), "neutrality": 1 if g.get("neutralSite") else 0}
            for g in data if g.get("id")]
    return d1_store.upsert_games(rows)


def do_lines(season: int) -> int:
    n = 0
    for wk in range(1, MAX_WEEK + 1):
        data = cfbd("lines", {"year": season, "week": wk, "seasonType": "regular"})
        rows = []
        for g in data or []:
            lines = g.get("lines") or []
            if not lines:
                continue
            L = lines[0]
            rows.append({"game_id": g.get("id"), "book": "cfbd",
                         "spread_home": L.get("spread"), "total": L.get("overUnder"),
                         "home_moneyline": L.get("homeMoneyline"),
                         "away_moneyline": L.get("awayMoneyline")})
        if rows:
            n += d1_store.upsert_closing_lines(rows)
    return n


def do_ratings(season: int) -> int:
    obs, now = [], _ts()
    for ep, key, field in [("ratings/elo", "elo", "elo"),
                           ("ratings/sp", "sp_plus", "rating"),
                           ("ratings/sp", "sp_plus_rk", "ranking"),
                           ("ratings/fpi", "fpi", "fpi"),
                           ("talent", "talent_rating", "talent"),
                           ("recruiting/teams", "recruiting_class_rank", "rank")]:
        for t in cfbd(ep, {"year": season}) or []:
            if t.get(field) is None:
                continue
            tid = _tid(t.get("team"))
            if tid is None:
                continue
            obs.append({"subject_type": "team", "subject_id": tid, "season": season,
                        "week": 0, "stat_key": key, "value": t.get(field),
                        "source": "cfbd", "recorded_at": now})
    return d1_store.upsert_stat_observations(obs)


def do_season_stats(season: int) -> int:
    now = _ts()
    obs = []
    for t in cfbd("stats/season", {"year": season}) or []:
        tid = _tid(t.get("team"))
        if tid is None or t.get("statValue") is None:
            continue
        try:
            val = float(t["statValue"])
        except (TypeError, ValueError):
            continue
        obs.append({"subject_type": "team", "subject_id": tid, "season": season, "week": 0,
                    "stat_key": t.get("statName"), "value": val, "source": "cfbd",
                    "recorded_at": now})
    return d1_store.upsert_stat_observations(obs)


JOBS = [("teams", seed_teams), ("games", do_games), ("lines", do_lines),
        ("ratings", do_ratings), ("season_stats", do_season_stats)]


# ----------------------------------------------------------------------- lock
def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import subprocess
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                 capture_output=True, text=True, timeout=15).stdout
            return str(pid) in out
        except Exception:  # noqa: BLE001
            return True          # cannot tell -> assume alive (safe)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _take_lock() -> bool:
    """Single-runner lock. A lock left by a DEAD process is stolen (that stale lock
    is what blocked the first resume after the turn-kill)."""
    if os.path.exists(LOCK):
        try:
            old = int(open(LOCK, encoding="utf-8").read().strip() or "0")
        except Exception:  # noqa: BLE001
            old = 0
        if old and old != os.getpid() and _pid_alive(old):
            print(f"REFUSING: lock {LOCK} held by live pid {old} (another backfill running).",
                  flush=True)
            return False
        print(f"stealing stale lock {LOCK} (pid {old} is gone)", flush=True)
    with open(LOCK, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    return True


# ------------------------------------------------------------------------ run
def run(seasons: list[int]) -> int:
    global SRC_ROWS, ROWS_WRITTEN
    if not _take_lock():
        return 2
    try:
        load_name_map(max(seasons) if seasons else SEASONS[-1])
        ck = _load()
        ck.setdefault("chunks", {})
        ck.setdefault("failed", {})
        ck.setdefault("no_data", [])
        for season in seasons:
            for name, fn in JOBS:
                tag = f"{season}:{name}"
                if tag in ck["done"]:
                    print(f"  skip {tag} (already done)", flush=True)
                    continue
                SRC_ROWS = 0
                try:
                    n = fn(season)
                except d1_store.BudgetExceeded as e:
                    print(f"  STOP: D1 daily write cap on {tag}: {e}", flush=True)
                    _save(ck)
                    print(f"  checkpoint saved; re-run to resume (rows_written={ROWS_WRITTEN}).",
                          flush=True)
                    return 0
                except Exception as e:  # noqa: BLE001 — a chunk that cannot confirm its
                    # writes is a FAILURE, never a silent 'done'.
                    ck.setdefault("failed", {})[tag] = f"{type(e).__name__}: {e}"
                    _save(ck)
                    print(f"  FAIL {tag}: {type(e).__name__}: {e}", flush=True)
                    print(f"  chunk NOT marked done; run aborted (rows_written={ROWS_WRITTEN}).",
                          flush=True)
                    return 1
                ROWS_WRITTEN += n
                if n == 0:
                    if SRC_ROWS == 0:
                        if tag not in ck["no_data"]:
                            ck["no_data"].append(tag)
                        _save(ck)
                        print(f"  no-data {tag}: source returned 0 rows -> nothing to write",
                              flush=True)
                        continue
                    ck.setdefault("failed", {})[tag] = (
                        f"0 confirmed writes for {SRC_ROWS} source rows")
                    _save(ck)
                    print(f"  FAIL {tag}: source returned {SRC_ROWS} rows but D1 confirmed 0 "
                          f"writes -> NOT marking done", flush=True)
                    return 1
                ck = _load()                       # re-read: survive a concurrent run
                ck.setdefault("done", []).append(tag)
                ck.setdefault("chunks", {})[tag] = {"confirmed": n, "at": _ts()}
                ck.get("failed", {}).pop(tag, None)
                _save(ck)
                print(f"  {tag}: {n} rows confirmed  [cfbd_calls={CALLS} "
                      f"rows_written={ROWS_WRITTEN} d1_today={d1_store.ledger_written()}]",
                      flush=True)
        print(f"\nDONE. done-chunks={len(_load()['done'])} cfbd_calls={CALLS} "
              f"rows_written={ROWS_WRITTEN} d1_today={d1_store.ledger_written()}", flush=True)
        if UNMATCHED:
            print(f"UNMATCHED team names (skipped): {sorted(set(UNMATCHED))}", flush=True)
        return 0
    finally:
        try:
            os.remove(LOCK)
        except OSError:
            pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", type=int, nargs="*", default=SEASONS)
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()
    if a.status:
        print(json.dumps(_load(), indent=2))
        return
    sys.exit(run(a.seasons))


if __name__ == "__main__":
    main()