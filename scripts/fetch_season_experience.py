#!/usr/bin/env python3
"""Fetch per-season roster experience so v2's 15% experience term has a history.

Provenance (app.py:_cfbd_roster_experience): experience_score = (avg_year - 1) / 3 * 100,
computed from CFBD /roster player years. Season-level and known at season start, so it is
leak-free as a season-long prior — but it has NO weekly variation and was never plumbed into
the backtest before this.

Writes data/backtest_cache/experience_<season>.json: {team: {roster_count, avg_year, experience_score}}
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
    raise SystemExit("CFBD_API_KEY not found in .env")


def fetch(year: int, key: str) -> list[dict]:
    url = f"https://api.collegefootballdata.com/roster?year={year}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def main() -> int:
    years = [int(a) for a in sys.argv[1:]] or [2021, 2022, 2023, 2024, 2025]
    key = api_key()
    CACHE.mkdir(parents=True, exist_ok=True)
    for year in years:
        try:
            players = fetch(year, key)
        except Exception as e:  # noqa: BLE001
            print(f"  {year}: FETCH FAILED {e}")
            continue
        by_team: dict[str, list] = {}
        for p in players:
            by_team.setdefault(p.get("team", ""), []).append(p)
        out = {}
        for team, plist in by_team.items():
            # CFBD mixes two different meanings in the same `year` field: class year (1-4) and
            # the SEASON year (e.g. 2024). ~5% of rows are season-year, and because 2024 is ~700x
            # larger than 3, even a handful silently inflates a team's mean into the thousands.
            # Keep only genuine class years.
            years_ = [p.get("year") for p in plist
                      if isinstance(p.get("year"), int) and 1 <= p.get("year") <= 4]
            avg = round(sum(years_) / len(years_), 2) if years_ else 0
            out[team] = {
                "roster_count": len(plist),
                "avg_year": avg,
                "experience_score": round((avg - 1) / 3 * 100, 1) if avg else 0,
            }
        dest = CACHE / f"experience_{year}.json"
        dest.write_text(json.dumps(out, indent=1), encoding="utf-8")
        vals = [v["experience_score"] for v in out.values() if v["experience_score"]]
        print(f"  {year}: {len(out)} teams, {len(players)} players, "
              f"experience_score range {min(vals):.1f}-{max(vals):.1f}, "
              f"mean {sum(vals)/len(vals):.1f} -> {dest.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())