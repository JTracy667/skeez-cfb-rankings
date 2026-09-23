#!/usr/bin/env python3
"""harvest_plays.py — ARM 2: weekly matchup metrics from CFBD /plays.

GATE FIRST (V5 work order): one probe call measured 12.0 MB / 19,574 rows for 2024 wk8, so a
full 5-season pull is ~75 calls and ~1.47M rows. That is feasible inside CFBD's remaining
monthly quota; it is NOT feasible to hold on disk, so raw plays are aggregated per week and
discarded. Cached output is metrics only.

WHAT IS COMPUTED, per team per week (point-in-time: a week only sees its OWN plays)
    off_plays / plays_per_game      scrimmage plays (down 1-4)
    off_ppa, epa_early              CFBD `ppa` (expected points added), all / downs 1-2
    epa_pass, epa_rush              by play type
    explosive_rate                  share of scrimmage plays gaining 20+ yards
    havoc_rate                      defensive: sacks, INTs, fumbles forced/recovered,
                                    blocked kicks, and negative-yard rushes
    def_ppa                         ppa allowed on defense
    sec_per_play                    measured from the play clock, NOT from the schedule:
                                    absolute game seconds = (period-1)*900 + (900 - clock);
                                    consecutive-play deltas > 60s are breaks and dropped

RESUMABLE at week granularity: data/playbyplay_YYYY.json is rewritten after each week, so a
killed process resumes where it stopped.

    python scripts/harvest_plays.py --seasons 2021 2022 2023 2024 2025
    python scripts/harvest_plays.py --seasons 2024 --weeks 1 2      # spot check
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data"
CFBD_BASE = "https://api.collegefootballdata.com"
MAX_WEEK = 16

PASS_TYPES = {"Pass Reception", "Pass Incompletion", "Passing Touchdown", "Sack",
              "Pass Interception Return", "Interception", "Interception Return Touchdown"}
RUSH_TYPES = {"Rush", "Rushing Touchdown"}
HAVOC_TYPES = {"Sack", "Interception", "Pass Interception Return",
               "Interception Return Touchdown", "Fumble Recovery (Opponent)",
               "Blocked Field Goal", "Blocked Punt", "Blocked Field Goal Touchdown"}
# Scrimmage plays only. Without this, kicks/punts/penalties/timeouts inflate plays-per-game by
# ~25% (measured: Tennessee 97 'down>0' plays vs ~70 real offensive snaps) and therefore also
# skew explosive_rate and havoc_rate, whose denominators are offensive/defensive play counts.
NON_OFFENSIVE = {"Punt", "Kickoff", "Kickoff Return (Offense)", "Field Goal Good",
                 "Field Goal Missed", "Blocked Field Goal", "Blocked Punt", "Timeout",
                 "Penalty", "End Period", "End of Half", "End of Game", "End of Regulation",
                 "Uncategorized", "Safety", "Blocked Field Goal Touchdown",
                 "Missed Field Goal Return", "Kickoff Return Touchdown"}


def key() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("CFBD_API_KEY"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("CFBD_API_KEY not found")


KEY = key()


def fetch_week(season: int, week: int) -> list[dict]:
    url = f"{CFBD_BASE}/plays?year={season}&week={week}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {KEY}", "User-Agent": "cfb-analytics/1.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503):
                time.sleep(5 * (attempt + 1))
                continue
            raise
        except Exception:  # noqa: BLE001
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"plays fetch failed {season} wk{week}")


def _abs_secs(period, clock) -> float | None:
    if not isinstance(clock, dict):
        return None
    p = period or 0
    mins, secs = clock.get("minutes") or 0, clock.get("seconds") or 0
    return (p - 1) * 900.0 + (900.0 - (mins * 60.0 + secs))


def aggregate(plays: list[dict]) -> dict:
    """week metrics per team -> {team: {...}}"""
    acc = defaultdict(lambda: defaultdict(float))
    last = {}          # gameId -> (abs_secs, offense) for pace
    per_game = defaultdict(set)

    for p in plays:
        off, dfn = p.get("offense"), p.get("defense")
        if not off or not dfn:
            continue
        down = p.get("down") or 0
        ptype = p.get("playType") or ""
        yards = p.get("yardsGained")
        ppa = p.get("ppa")
        game = p.get("gameId")
        per_game[off].add(game)
        is_scrimmage = down > 0 and ptype not in NON_OFFENSIVE

        if is_scrimmage:                                 # real offensive snap
            acc[off]["off_plays"] += 1
            acc[dfn]["def_plays"] += 1
            if isinstance(ppa, (int, float)):
                acc[off]["off_ppa_sum"] += ppa
                acc[off]["off_ppa_n"] += 1
                acc[dfn]["def_ppa_sum"] += ppa
                acc[dfn]["def_ppa_n"] += 1
                if down in (1, 2):
                    acc[off]["epa_early_sum"] += ppa
                    acc[off]["epa_early_n"] += 1
                if ptype in PASS_TYPES:
                    acc[off]["epa_pass_sum"] += ppa
                    acc[off]["epa_pass_n"] += 1
                    acc[dfn]["def_epa_pass_sum"] += ppa
                    acc[dfn]["def_epa_pass_n"] += 1
                elif ptype in RUSH_TYPES:
                    acc[off]["epa_rush_sum"] += ppa
                    acc[off]["epa_rush_n"] += 1
                    acc[dfn]["def_epa_rush_sum"] += ppa
                    acc[dfn]["def_epa_rush_n"] += 1
            if isinstance(yards, (int, float)) and yards >= 20:
                acc[off]["explosive"] += 1

        # defensive havoc: created by the DEFENSE
        havoc = ptype in HAVOC_TYPES or (ptype in RUSH_TYPES and isinstance(yards, (int, float)) and yards < 0)
        if havoc and is_scrimmage:
            acc[dfn]["havoc"] += 1

        # pace: measured from the play clock, per game, attributed to the offense
        s = _abs_secs(p.get("period"), p.get("clock"))
        if s is not None and game is not None:
            prev = last.get(game)
            if prev is not None:
                delta = s - prev[0]
                if 0 < delta <= 60:
                    acc[prev[1]]["sec_sum"] += delta
                    acc[prev[1]]["sec_n"] += 1
            last[game] = (s, off)

    out = {}
    for team, a in acc.items():
        n_off = a.get("off_plays") or 0
        n_def = a.get("def_plays") or 0
        games = max(1, len(per_game.get(team, {1})))
        out[team] = {
            "games": games,
            "off_plays": int(n_off),
            "plays_per_game": round(n_off / games, 2),
            "off_ppa": round(a["off_ppa_sum"] / a["off_ppa_n"], 4) if a.get("off_ppa_n") else None,
            "epa_early": round(a["epa_early_sum"] / a["epa_early_n"], 4) if a.get("epa_early_n") else None,
            "epa_pass": round(a["epa_pass_sum"] / a["epa_pass_n"], 4) if a.get("epa_pass_n") else None,
            "epa_rush": round(a["epa_rush_sum"] / a["epa_rush_n"], 4) if a.get("epa_rush_n") else None,
            "explosive_rate": round(a.get("explosive", 0) / n_off, 4) if n_off else None,
            "havoc_rate": round(a.get("havoc", 0) / n_def, 4) if n_def else None,
            "def_ppa": round(a["def_ppa_sum"] / a["def_ppa_n"], 4) if a.get("def_ppa_n") else None,
            "def_epa_pass": (round(a["def_epa_pass_sum"] / a["def_epa_pass_n"], 4)
                             if a.get("def_epa_pass_n") else None),
            "def_epa_rush": (round(a["def_epa_rush_sum"] / a["def_epa_rush_n"], 4)
                             if a.get("def_epa_rush_n") else None),
            "sec_per_play": round(a["sec_sum"] / a["sec_n"], 2) if a.get("sec_n") else None,
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", type=int, nargs="+", default=[2021, 2022, 2023, 2024, 2025])
    ap.add_argument("--weeks", type=int, nargs="+", default=list(range(1, MAX_WEEK + 1)))
    ap.add_argument("--force", action="store_true", help="re-fetch weeks already cached")
    args = ap.parse_args()

    for season in args.seasons:
        path = OUT / f"playbyplay_{season}.json"
        doc = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"season": season, "weeks": {}}
        for week in args.weeks:
            wkey = str(week)
            if wkey in doc["weeks"] and not args.force:
                continue
            t0 = time.time()
            plays = fetch_week(season, week)
            if not plays:
                print(f"[plays] {season} wk{week}: 0 rows (bye/off-week) — recording empty")
                doc["weeks"][wkey] = {}
                path.write_text(json.dumps(doc, indent=1), encoding="utf-8")
                continue
            doc["weeks"][wkey] = aggregate(plays)
            path.write_text(json.dumps(doc, indent=1), encoding="utf-8")
            print(f"[plays] {season} wk{week}: {len(plays):,} rows -> {len(doc['weeks'][wkey])} teams "
                  f"({time.time() - t0:.1f}s)  cached {path.name}")
    print("[plays] done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())