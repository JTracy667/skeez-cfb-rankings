#!/usr/bin/env python3
"""harvest_odds_history.py — OPEN/CLOSE line snapshots from The Odds API historical endpoint.

CADENCE (planned before executing, per the work order)
------------------------------------------------------
Snapshots are placed from OUR OWN frozen game dates (data/backtest_cache/lines_<season>.json),
not from a guessed calendar, so every snapshot lands where the games actually are:

  OPEN   earliest game date of the week - 5 days, 16:00Z  (~Mon 12:00 ET)
  CLOSE  the week's modal game date (usually Saturday), 14:00Z  (~10:00 ET, ~2h pre-kick)

Weeks containing a Thursday/Friday game also get a MID snapshot (2 days before the earliest game,
22:00Z) so weeknight games are not left without a pre-kickoff line.

COST — corrected against the live API, not assumed
--------------------------------------------------
The historical endpoint charges **10 credits PER MARKET**; earlier notes said 10/call, which was
wrong (that was a 1-market probe). `markets=spreads,totals` therefore costs **20 credits**.

    2 snapshots x 75 week-seasons                      = 150 calls x 20 = 3,000 credits
    + MID on weeks with weeknight games (~75 weeks)    =  75 calls x 20 = 1,500 credits
    ------------------------------------------------------------------------------
    total                                                             ~4,500 credits
    cap set by the work order                                          7,500 credits

Runs are RESUMABLE: an existing output file is skipped, so an interrupted harvest just resumes.

Usage:
    python scripts/harvest_odds_history.py --smoke         # 1 week, verify parsing
    python scripts/harvest_odds_history.py --all           # full 2021-2025 harvest
    python scripts/harvest_odds_history.py --season 2023 --week 4
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "backtest_cache"
OUTDIR = ROOT / "data" / "odds_history"
OA = "https://api.the-odds-api.com/v4"
UA = "skeezcfb-rankings-harvest/1.0 (+https://skeezcfb-rankings.com)"
SEASONS = [2021, 2022, 2023, 2024, 2025]
# Jeff's actual books. VERIFIED against the live feed's own titles (2026-09-23):
#   betonlineag  -> "BetOnline.ag"  (his primary sharp read)
#   betmgm       -> "BetMGM"
#   williamhill_us -> titled "Caesars"  ** NOT William Hill **  The key is a legacy name; the feed
#                     sells Caesars lines under it. There is no William Hill key in ANY region.
#   circa        -> NOT CARRIED by this API (no `circa` key across us/eu/uk)
# Do not describe williamhill_us as "William Hill" in any output - it is Caesars.
# pinnacle was dropped by request — and it is EU-region only, so removing it also keeps this
# harvest US-only and at half the credit cost.
WRITE_BOOKS = ("betonlineag", "betmgm", "williamhill_us",
               "draftkings", "fanduel", "bovada", "lowvig")

ENV = {}
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1)
        ENV[k.strip()] = v.strip().strip('"').strip("'")
KEY = ENV.get("THE_ODDS_API_KEY", "")

COST_PER_SNAPSHOT = 20  # 2 markets x 10


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def plan(season: int) -> list[dict]:
    """Snapshot plan for a season, derived from the frozen game dates."""
    lines = json.loads((CACHE / f"lines_{season}.json").read_text(encoding="utf-8"))
    by_week: dict[int, list[datetime]] = {}
    for g in lines:
        if not g.get("startDate"):
            continue
        by_week.setdefault(int(g.get("week") or 0), []).append(_dt(g["startDate"]))
    out = []
    for wk in sorted(by_week):
        if wk < 1:
            continue
        dates = sorted(by_week[wk])
        first, last = dates[0], dates[-1]
        # modal (most common) calendar day = the main slate
        counts: dict = {}
        for d in dates:
            counts[d.date()] = counts.get(d.date(), 0) + 1
        modal = max(counts, key=lambda k: counts[k])
        shots = [
            ("open", (first - timedelta(days=5)).replace(hour=16, minute=0, second=0, microsecond=0)),
            ("close", datetime(modal.year, modal.month, modal.day, 14, 0, tzinfo=timezone.utc)),
        ]
        # weeknight games (earlier than the modal day) get a pre-kick MID snapshot
        if first.date() < modal:
            shots.append(("mid", (first - timedelta(days=2)).replace(
                hour=22, minute=0, second=0, microsecond=0)))
        for label, ts in shots:
            out.append({"season": season, "week": wk, "label": label,
                        "ts": ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    return out


def fetch(ts: str) -> tuple[dict | None, dict]:
    url = (f"{OA}/historical/sports/americanfootball_ncaaf/odds?apiKey={KEY}"
           f"&date={ts}&regions=us&markets=spreads,totals&oddsFormat=american")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            d = json.load(r)
            q = {k: v for k, v in r.headers.items() if "requests" in k.lower()}
        return d, q
    except urllib.error.HTTPError as e:
        body = None
        try:
            body = json.loads(e.read())
        except Exception:  # noqa: BLE001
            pass
        return {"__error__": e.code, "body": body}, {k: v for k, v in e.headers.items()
                                                    if "requests" in k.lower()}


def slim(data: dict, ts: str) -> dict:
    """Keep book/line detail plus the fields needed to match a snapshot to a game."""
    events = []
    for ev in data.get("data", []):
        books = {}
        for b in ev.get("bookmakers", []):
            if b.get("key") not in WRITE_BOOKS:
                continue
            mkt = {}
            for m in b.get("markets", []):
                if m.get("key") in ("spreads", "totals"):
                    mkt[m["key"]] = [{"name": o.get("name"), "price": o.get("price"),
                                      "point": o.get("point")} for o in m.get("outcomes", [])]
            if mkt:
                books[b["key"]] = {"last_update": b.get("last_update"), **mkt}
        events.append({"id": ev.get("id"), "commence_time": ev.get("commence_time"),
                       "home_team": ev.get("home_team"), "away_team": ev.get("away_team"),
                       "bookmakers": books})
    return {"snapshot_ts": ts, "api_timestamp": data.get("timestamp"),
            "previous_timestamp": data.get("previous_timestamp"),
            "next_timestamp": data.get("next_timestamp"), "n_events": len(events),
            "events": events}


def run_one(season: int, week: int, shots: list[dict], spent: list[int]) -> None:
    dest = OUTDIR / f"{season}_wk{week:02d}.json"
    if dest.exists():
        print(f"  skip {dest.name} (exists)")
        return
    payload = {"season": season, "week": week, "snapshots": []}
    for s in shots:
        d, q = fetch(s["ts"])
        spent[0] += COST_PER_SNAPSHOT
        if d is None or "__error__" in d:
            print(f"  !! {season} wk{week} {s['label']} {s['ts']} -> {d} quota={q}")
            continue
        payload["snapshots"].append({"label": s["label"], **slim(d, s["ts"])})
        print(f"  {season} wk{week:<2} {s['label']:<5} {s['ts']} "
              f"events={len(d.get('data', []))} spent~{spent[0]}")
        time.sleep(0.4)
    if payload["snapshots"]:
        dest.write_text(json.dumps(payload, indent=1), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int)
    ap.add_argument("--week", type=int)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--smoke", action="store_true")
    args = ap.parse_args()
    OUTDIR.mkdir(parents=True, exist_ok=True)
    spent = [0]

    if args.smoke:
        pl = plan(2025)
        for wk in ({pl[0]["week"], pl[3]["week"]}):
            shots = [s for s in pl if s["week"] == wk]
            print(f"[smoke] {2025} wk{wk}: {[ (s['label'], s['ts']) for s in shots ]}")
            run_one(2025, wk, shots, spent)
        print(f"[smoke] estimated spend ~{spent[0]} credits")
        return 0

    targets = ([(args.season, args.week)] if args.season and args.week
               else [(s, w) for s in SEASONS for w in sorted({p["week"] for p in plan(s)})])
    pl_by = {s: plan(s) for s in SEASONS}
    total_planned = sum(COST_PER_SNAPSHOT * len(pl_by[s]) for s in SEASONS)
    print(f"[harvest] {len(targets)} week-seasons; full-run plan ~{total_planned} credits "
          f"(cap 7,500)")
    for season, week in targets:
        shots = [p for p in pl_by[season] if p["week"] == week]
        run_one(season, week, shots, spent)
    print(f"[harvest] done; spent ~{spent[0]} credits this run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())