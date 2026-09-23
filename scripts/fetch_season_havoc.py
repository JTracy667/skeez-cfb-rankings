#!/usr/bin/env python3
"""Fetch per-season havoc (CFBD has no native havoc stat — all components are in /stats/season).

Formula copied verbatim from app.py:_cfbd_team_stats:
  def_havoc     = (tacklesForLoss + sacks + interceptionsOpponent + fumblesLostOpponent)
                  / (passAttemptsOpponent + rushingAttemptsOpponent)
  havoc_allowed = (tacklesForLossOpponent + sacksOpponent + interceptions + fumblesLost)
                  / (passAttempts + rushingAttempts)

Season Y-1's havoc is used as season Y's prior: it is known before season Y starts, so it is
leak-free. In-season havoc has no point-in-time source (D1 stores the components at week 0 only),
so this prior-season value is the only non-leaky option without a play-by-play harvest.

Writes data/backtest_cache/havoc_<year>.json -> {team: {def_havoc, havoc_allowed, plays, opp_plays}}
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "backtest_cache"


def api_key() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("CFBD_API_KEY"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("CFBD_API_KEY not found")


def main() -> int:
    years = [int(a) for a in sys.argv[1:]] or [2020, 2021, 2022, 2023, 2024]
    key = api_key()
    CACHE.mkdir(parents=True, exist_ok=True)
    for year in years:
        url = f"https://api.collegefootballdata.com/stats/season?year={year}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                rows = json.load(r)
        except Exception as e:  # noqa: BLE001
            print(f"  {year}: FETCH FAILED {e}")
            continue
        teams: dict[str, dict] = {}
        for row in rows:
            teams.setdefault(row["team"], {})[row["statName"]] = row["statValue"]
        out = {}
        for team, s in teams.items():
            plays = (s.get("passAttempts", 0) or 0) + (s.get("rushingAttempts", 0) or 0)
            opp_plays = (s.get("passAttemptsOpponent", 0) or 0) + (s.get("rushingAttemptsOpponent", 0) or 0)
            dh = ha = None
            if opp_plays:
                dh = round((s.get("tacklesForLoss", 0) + s.get("sacks", 0)
                            + s.get("interceptionsOpponent", 0)
                            + s.get("fumblesLostOpponent", 0)) / opp_plays, 4)
            if plays:
                ha = round((s.get("tacklesForLossOpponent", 0) + s.get("sacksOpponent", 0)
                            + s.get("interceptions", 0) + s.get("fumblesLost", 0)) / plays, 4)
            out[team] = {"def_havoc": dh, "havoc_allowed": ha,
                         "plays": plays, "opp_plays": opp_plays}
        dest = CACHE / f"havoc_{year}.json"
        dest.write_text(json.dumps(out, indent=1), encoding="utf-8")
        vals = [v["def_havoc"] for v in out.values() if v["def_havoc"] is not None]
        allwd = [v["havoc_allowed"] for v in out.values() if v["havoc_allowed"] is not None]
        print(f"  {year}: {len(out)} teams; def_havoc {min(vals):.3f}-{max(vals):.3f} "
              f"(mean {sum(vals)/len(vals):.3f}); havoc_allowed {min(allwd):.3f}-{max(allwd):.3f} "
              f"(mean {sum(allwd)/len(allwd):.3f}) -> {dest.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())