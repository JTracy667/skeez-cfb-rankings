#!/usr/bin/env python
"""Upsert ONE season's games (with finals) into D1 `games`. One CFBD call.

WHY THIS EXISTS (F1)
D1 `games` had finals only through week 3 while the site served week 5: results are graded
from a CFBD fetch and written to `data/finals_cache.json`, a file on the Cloudflare
container's EPHEMERAL disk. So results were never durable and D1 — the store the parity
test asserts against — silently fell behind. This is the surgical refill.

Uses the SAME field mapping as `scripts/backfill_d1.py::do_games` on purpose: two mappings
that drift is how a dataset goes inconsistent.

Usage:  python scripts/refresh_d1_games.py [season]     (default: current CFBD_YEAR/2026)
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import d1_store  # noqa: E402

CFBD_BASE = "https://api.collegefootballdata.com"


def _api_key() -> str:
    key = os.environ.get("CFB_API_KEY")
    if key:
        return key
    env = REPO / ".env"
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith("CFB_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"')
    raise SystemExit("CFB_API_KEY not found in environment or .env")


def fetch_games(season: int) -> list[dict]:
    url = f"{CFBD_BASE}/games?" + urllib.parse.urlencode({"year": season})
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {_api_key()}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def to_rows(games: list[dict]) -> list[dict]:
    return [{"game_id": g["id"], "season": g.get("season"), "week": g.get("week"),
             "home_id": g.get("homeId"), "away_id": g.get("awayId"),
             "kickoff": g.get("startDate"), "home_score": g.get("homePoints"),
             "away_score": g.get("awayPoints"),
             "status": "final" if g.get("completed") else "scheduled",
             "venue": g.get("venue"), "neutrality": 1 if g.get("neutralSite") else 0}
            for g in games if g.get("id")]


def main() -> int:
    season = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("CFBD_YEAR", 2026))
    games = fetch_games(season)
    rows = to_rows(games)
    if not rows:
        print(f"no games returned for {season}")
        return 1
    written = d1_store.upsert_games(rows)
    scored = [r for r in rows if r["home_score"] is not None]
    weeks = sorted({r["week"] for r in scored if r["week"] is not None})
    print(f"season {season}: {len(rows)} games fetched, {len(scored)} with finals, "
          f"{written} rows written; newest scored week = {weeks[-1] if weeks else None}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())