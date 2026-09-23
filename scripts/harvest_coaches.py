#!/usr/bin/env python3
"""harvest_coaches.py — per-season coach records from CFBD /coaches (ARM 2 input).

WHY THIS EXISTS: `coach_win_pct` appears in data/cfbd_analytics.json, but that file is a
CURRENT single snapshot with no season key, so it cannot be used on the 2021-25 frozen set
without leaking the present into the past. There is no per-season coaches cache in the repo.
This fetches /coaches?year=YYYY (one call per season, 6 calls total) and caches the head-coach
record per team, so season Y can be described using season Y-1's coach record — point in time.

    python scripts/harvest_coaches.py --years 2020 2021 2022 2023 2024 2025
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "backtest_cache"
CFBD_BASE = "https://api.collegefootballdata.com"


def key() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("CFBD_API_KEY"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("CFBD_API_KEY not found")


KEY = key()


def fetch(year: int) -> list[dict]:
    url = f"{CFBD_BASE}/coaches?year={year}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {KEY}", "User-Agent": "cfb-analytics/1.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503):
                time.sleep(5 * (attempt + 1))
                continue
            raise
        except Exception:  # noqa: BLE001
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"coaches fetch failed {year}")


def head_coach(rec: dict, year: int) -> tuple[str, dict] | None:
    """The coach of record for `year` at each school: most games that season, tie-break on wins.

    NOTE: /coaches nests the school inside seasons[] ("school"), there is no top-level team key —
    the first version of this script returned 0 coaches because it read rec["team"].
    """
    best: dict[str, dict] = {}
    for c in rec.get("seasons") or []:
        if c.get("year") != year:
            continue
        school = c.get("school")
        if not school:
            continue
        g = c.get("games") or 0
        cur = best.get(school)
        if cur is None or (g, c.get("wins") or 0) > (cur.get("games") or 0, cur.get("wins") or 0):
            wp = c.get("winPercentage")
            if wp is None and g:
                wp = round((c.get("wins") or 0) / g, 4)
            best[school] = {"coach": (rec.get("firstName", "") + " " + rec.get("lastName", "")).strip(),
                            "games": g, "wins": c.get("wins") or 0, "losses": c.get("losses") or 0,
                            "win_pct": wp}
    return best if best else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, nargs="+", default=[2020, 2021, 2022, 2023, 2024, 2025])
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    for year in args.years:
        path = OUT / f"coaches_{year}.json"
        if path.exists() and not args.force:
            print(f"[coaches] {year}: cached ({path.name})")
            continue
        rows = fetch(year)
        out: dict[str, dict] = {}
        for rec in rows:
            got = head_coach(rec, year)
            if not got:
                continue
            for school, hc in got.items():
                if hc.get("win_pct") is None:
                    continue
                cur = out.get(school)
                if cur is None or (hc["games"], hc["wins"]) > (cur["games"], cur["wins"]):
                    out[school] = hc
        path.write_text(json.dumps(out, indent=1), encoding="utf-8")
        print(f"[coaches] {year}: {len(rows)} coaches -> {len(out)} head coaches  cached {path.name}")
    print("[coaches] done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())