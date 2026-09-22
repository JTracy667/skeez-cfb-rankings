#!/usr/bin/env python3
"""calibrate_fcs_prior.py — what IS an FCS opponent worth?

Replaces two hand-tuned stop-gaps in project_score_multi_factor:
    fcs_composite = 16.0          (level of an FCS opponent)
    FCS_BLOWOUT_BOOST = 15.0      (extra pts added for FBS-vs-FCS games)
Both were added before any FCS data existed and are marked "revisit when FCS data
lands". This measures the real thing instead.

STAGE 1 (this script): measure. For every FBS-vs-FCS game in the archive, report
the actual margin, split by the FCS opponent's strength evidence:
  * poll-ranked FCS opponent (fcs_rating = FCS Coaches Poll rank 1..25)
  * unranked FCS opponent
A real rating must reproduce these margins; a flat constant cannot, because the
two groups differ.

Reads the LOCAL D1 export (fast, no D1 read quota).
"""
from __future__ import annotations

import os
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

EXPORT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "data", "d1_export.sqlite")
SEASONS = (2021, 2022, 2023, 2024, 2025)


def main() -> int:
    if not os.path.exists(EXPORT):
        print(f"missing export: {EXPORT}\nrun: python scripts/export_d1.py")
        return 1
    c = sqlite3.connect(EXPORT)
    c.row_factory = sqlite3.Row

    cls = {r["team_id"]: (r["classification"] or "").lower()
           for r in c.execute("SELECT team_id, classification FROM teams")}
    print(f"teams loaded: {len(cls)}  (fcs={sum(1 for v in cls.values() if v == 'fcs')})")

    # FCS coaches-poll rank per (team, season). Poll rank 1 is the BEST.
    poll = {}
    for r in c.execute("""SELECT subject_id, season, MIN(value) v FROM stat_observations
                          WHERE stat_key='fcs_rating' GROUP BY subject_id, season"""):
        poll[(r["subject_id"], r["season"])] = int(r["v"])

    q = f"""SELECT season, week, home_id, away_id, home_score, away_score
            FROM games
            WHERE season IN {SEASONS} AND home_score IS NOT NULL AND away_score IS NOT NULL"""
    fbs_vs_fcs = []
    fcs_vs_fcs = 0
    for g in c.execute(q):
        hc, ac = cls.get(g["home_id"]), cls.get(g["away_id"])
        if hc == "fcs" and ac == "fcs":
            fcs_vs_fcs += 1
            continue
        if hc == "fbs" and ac == "fcs":
            fbs_side, fcs_side, sign = g["home_id"], g["away_id"], 1
        elif ac == "fbs" and hc == "fcs":
            fbs_side, fcs_side, sign = g["away_id"], g["home_id"], -1
        else:
            continue
        margin_fbs = sign * (g["home_score"] - g["away_score"])
        fbs_vs_fcs.append({"season": g["season"], "week": g["week"], "fbs": fbs_side,
                           "fcs": fcs_side, "margin_fbs": margin_fbs,
                           "total": g["home_score"] + g["away_score"],
                           "rank": poll.get((fcs_side, g["season"]))})

    print(f"\nFBS-vs-FCS games 2021-2025: {len(fbs_vs_fcs)}   (FCS-vs-FCS skipped: {fcs_vs_fcs})")
    if not fbs_vs_fcs:
        return 1

    def stats(rows, label):
        if not rows:
            print(f"  {label:<34} n=0")
            return
        m = sorted(r["margin_fbs"] for r in rows)
        t = sorted(r["total"] for r in rows)
        n = len(m)
        mean = sum(m) / n
        med = m[n // 2]
        print(f"  {label:<34} n={n:<5} margin mean={mean:>6.1f} median={med:>4} "
              f"p10={m[n//10]:>5} p90={m[9*n//10]:>5}   total mean={sum(t)/n:>5.1f}")

    print("\nFBS margin over the FCS opponent (+ = FBS won by that many):")
    stats(fbs_vs_fcs, "ALL FBS-vs-FCS")
    stats([r for r in fbs_vs_fcs if r["rank"] is not None], "vs POLL-RANKED FCS")
    stats([r for r in fbs_vs_fcs if r["rank"] is None], "vs unranked FCS")

    print("\nby FCS poll rank (rank 1 = best FCS team):")
    byr = defaultdict(list)
    for r in fbs_vs_fcs:
        if r["rank"] is not None:
            byr[r["rank"]].append(r["margin_fbs"])
    for rank in sorted(byr):
        v = sorted(byr[rank])
        print(f"   rank {rank:>2}: n={len(v):<4} mean margin={sum(v)/len(v):>6.1f}")

    print("\nby FBS week (early season is where the flat prior hurts most):")
    byw = defaultdict(list)
    for r in fbs_vs_fcs:
        byw[r["week"]].append(r["margin_fbs"])
    for wk in sorted(byw)[:8]:
        v = byw[wk]
        print(f"   week {wk:>2}: n={len(v):<4} mean margin={sum(v)/len(v):>6.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())