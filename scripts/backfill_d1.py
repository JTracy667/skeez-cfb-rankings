#!/usr/bin/env python3
"""backfill_d1.py — paced 5-season backfill into D1 `cfb-history` (spec §4).

Scope (spec §7.2): 2021-2025 full seasons + 2026 weeks 1-3. TEAM-LEVEL ONLY
(players deferred per spec §7.1).

FIXES over v1 (2026-09-21, both found by post-run verification):
  * CFBD ratings/stats endpoints key by team NAME, not teamId -> build a
    name->team_id map from /teams and REQUIRE a match. Unmatched names are
    counted and reported, never silently written with a NULL id.
  * A chunk is only marked done if it wrote rows -> no more "season_stats done,
    0 rows written".
  * Single-runner LOCK file -> two backfills cannot race the checkpoint.
  * cfbd_calls counted in memory; checkpoint saved once per chunk (kills the
    load/save race that produced duplicate 'done' entries).

Guarantees: checkpoint per (season, endpoint); resume never restart; idempotent;
D1 90K row-writes/day guard; CFBD budget counter logged per run.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import d1_store  # noqa: E402
import app as appmod  # noqa: E402 — reuse the LIVE app's CFBD client + team matcher

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CKPT = os.path.join(REPO, "data", "backfill_checkpoint.json")
LOCK = os.path.join(REPO, "data", "backfill.lock")
CFBD_BASE = "https://api.collegefootballdata.com"
SEASONS = [2021, 2022, 2023, 2024, 2025, 2026]
THROTTLE = 0.30
MAX_WEEK = 16

KEY = None
CALLS = 0
NAME2ID: dict[str, int] = {}
UNMATCHED: list[str] = []


def cfbd(path: str, params: dict | None = None, tries: int = 4):
    """Thin adapter over the LIVE APP's CFBD client (app._cfbd_get) — no private
    HTTP/auth layer. `params` map onto the client's extra query kwargs."""
    global CALLS
    p = dict(params or {})
    year = p.pop("year", SEASONS[-1])
    CALLS += 1
    time.sleep(THROTTLE)          # backfill-specific pacing (the app polls hourly)
    return appmod._cfbd_get(path, year=year, **p)


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
    if not c.get("started"):
        c["started"] = datetime.now(timezone.utc).isoformat()
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
    """REUSE the live app's team matcher (app._cfbd_teams -> {school: team}).
    Loaded independently of the teams chunk so a resumed run still maps names."""
    for school, t in (appmod._cfbd_teams() or {}).items():
        if t.get("id"):
            NAME2ID.setdefault(school, t["id"])
    print(f"  name->id map loaded from app._cfbd_teams(): {len(NAME2ID)} teams")


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
    obs, now = [], datetime.now(timezone.utc).isoformat()
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
    now = datetime.now(timezone.utc).isoformat()
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


def run(seasons: list[int]) -> None:
    if os.path.exists(LOCK):
        print(f"REFUSING: lock {LOCK} exists (another backfill running?). Remove to force.")
        return
    with open(LOCK, "w") as f:
        f.write(str(os.getpid()))
    try:
        load_name_map(max(seasons) if seasons else SEASONS[-1])
        ck = _load()
        for season in seasons:
            for name, fn in JOBS:
                tag = f"{season}:{name}"
                if tag in ck["done"]:
                    print(f"  skip {tag} (already done)")
                    continue
                try:
                    n = fn(season)
                except d1_store.BudgetExceeded as e:
                    print(f"  STOP: D1 budget hit on {tag}: {e}")
                    print("  checkpoint saved; re-run tomorrow to resume.")
                    return
                if n == 0:
                    print(f"  WARN {tag}: wrote 0 rows -> NOT marking done")
                    _save(ck)
                    continue
                ck = _load()
                ck.setdefault("done", []).append(tag)
                _save(ck)
                print(f"  {tag}: {n} rows  [cfbd_calls={CALLS}]")
        print(f"\nDONE. done-chunks={len(_load()['done'])} cfbd_calls={CALLS}")
        if UNMATCHED:
            print(f"UNMATCHED team names (skipped): {sorted(set(UNMATCHED))}")
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
    run(a.seasons)


if __name__ == "__main__":
    main()