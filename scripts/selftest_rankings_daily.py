#!/usr/bin/env python3
"""selftest_rankings_daily.py — prove the daily archive holds ONE row per team.

Guards the regression that produced 26 rows for a single date with two different
teams at rank 25 (two same-day writes unioned, caused by an ephemeral once-a-day
marker file in the container).

Checks:
  1. a write of N teams yields exactly N rows for that date (partition replaced)
  2. the D1-based day guard then reports "already written" and writes 0 more
  3. no duplicate (date, team_id) rows exist

Run:  python scripts/selftest_rankings_daily.py
Exit 0 = all pass.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import d1_store        # noqa: E402
import d1_write_path   # noqa: E402

FAILS: list[str] = []
N = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global N
    N += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        FAILS.append(label)


def live_teams() -> list[dict]:
    req = urllib.request.Request("https://skeezcfb-rankings.com/api/rankings",
                                 headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read().decode("utf-8"))["teams"]


def rows_for(today: str) -> list[dict]:
    return d1_store.query(
        "SELECT team_id, rank, composite FROM rankings_daily WHERE date = ?", [today])


def main() -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    teams = live_teams()
    print(f"\nlive teams: {len(teams)} | date: {today}")

    before = rows_for(today)
    print(f"rows before: {len(before)}")
    if len(before) > len(teams):
        dupes = {}
        for r in before:
            dupes.setdefault(r.get("rank"), []).append(r.get("team_id"))
        clash = {k: v for k, v in dupes.items() if len(v) > 1}
        print(f"  pre-existing anomaly: {len(before)} rows for {len(teams)} teams, "
              f"ranks with >1 team: {clash}")

    # 1. partition replace => exactly one row per team
    print("\n1. rewrite the day: expect exactly %d rows" % len(teams))
    n = d1_write_path.snapshot_rankings(teams, 2026, None)
    after = rows_for(today)
    check("row count == team count", len(after) == len(teams),
          f"{len(after)} rows vs {len(teams)} teams (write reported {n})")

    # 2. no duplicate (date, team_id)
    ids = [r.get("team_id") for r in after]
    check("no duplicate team_id in the date partition", len(ids) == len(set(ids)),
          f"{len(ids) - len(set(ids))} dupes")

    # 3. no rank held by two different teams
    by_rank: dict = {}
    for r in after:
        by_rank.setdefault(r.get("rank"), set()).add(r.get("team_id"))
    multi = {k: v for k, v in by_rank.items() if len(v) > 1}
    check("no rank shared by two teams", not multi, f"{multi}" if multi else "")

    # 4. the durable day guard now refuses to write again
    print("\n2. durable day guard (D1, survives container recycle)")
    n2 = d1_write_path.daily_rankings(lambda: live_teams(), 2026, None)
    check("second call same day writes nothing", n2 == 0, f"wrote {n2}")

    # 5. sorted sanity: composite descending by rank
    ordered = sorted(after, key=lambda r: r.get("rank") or 999)
    comps = [r.get("composite") for r in ordered if r.get("composite") is not None]
    check("composites descend with rank", all(comps[i] >= comps[i + 1] for i in range(len(comps) - 1)))

    print(f"\n{N - len(FAILS)}/{N} PASS")
    if FAILS:
        print("FAILED: " + "; ".join(FAILS))
        return 1
    print("rankings_daily: one consistent snapshot per date — VERIFIED")
    return 0


if __name__ == "__main__":
    sys.exit(main())