#!/usr/bin/env python3
"""fit_fcs_composite.py — the ACTUAL FCS composite, fitted from real games.

The margin formula is:  margin = (comp_home - comp_away) + 2.5*HFA + boost
so the composite gap maps 1:1 onto points and the FCS composite is identifiable
directly:

    comp_fcs = comp_fbs + HFA - actual_margin_from_fbs

This fits that on 2026 games (current season), because it is the only season for
which we have REAL composites for the FBS side. Splits by whether the FCS opponent
carries a poll rank, which is the differentiation the flat prior cannot express.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.request
from collections import defaultdict

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
EXPORT = os.path.join(_ROOT, "data", "d1_export.sqlite")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/140.0.0.0"


def live_composites() -> dict:
    req = urllib.request.Request("https://skeezcfb-rankings.com/api/analytics",
                                 headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    out = {}
    for t in (d.get("teams") or []):
        nm = t.get("school") or t.get("team") or t.get("name")
        comp = t.get("composite")
        if nm and isinstance(comp, (int, float)):
            out[str(nm).strip().lower()] = float(comp)
    return out


def main() -> int:
    comps = live_composites()
    print(f"live composites: {len(comps)} teams\n")

    c = sqlite3.connect(EXPORT)
    c.row_factory = sqlite3.Row
    teams = {r["team_id"]: r for r in c.execute("SELECT team_id, name, classification FROM teams")}
    poll = {(r["subject_id"], r["season"]): int(r["v"]) for r in c.execute(
        "SELECT subject_id, season, MIN(value) v FROM stat_observations WHERE stat_key='fcs_rating' GROUP BY subject_id, season")}

    games = c.execute("""SELECT season, week, home_id, away_id, home_score, away_score, neutrality
                         FROM games WHERE season=2026 AND home_score IS NOT NULL
                         AND away_score IS NOT NULL""").fetchall()
    print(f"2026 completed games: {len(games)}")

    rows, skipped = [], 0
    for g in games:
        hc_t, ac_t = teams.get(g["home_id"]), teams.get(g["away_id"])
        if not hc_t or not ac_t:
            continue
        h_cls = (hc_t["classification"] or "").lower()
        a_cls = (ac_t["classification"] or "").lower()
        if h_cls == "fbs" and a_cls == "fcs":
            fbs_name, fcs_name, fcs_id, sign, hfa = hc_t["name"], ac_t["name"], g["away_id"], 1, 2.5
        elif a_cls == "fbs" and h_cls == "fcs":
            fbs_name, fcs_name, fcs_id, sign, hfa = ac_t["name"], hc_t["name"], g["home_id"], -1, 2.5
        else:
            continue
        fc = comps.get(str(fbs_name).strip().lower())
        if fc is None:
            skipped += 1
            continue
        neutral = bool(g["neutrality"]) if g["neutrality"] is not None else False
        margin_fbs = sign * (g["home_score"] - g["away_score"])
        implied = fc + (0.0 if neutral else hfa) - margin_fbs
        rows.append({"fbs": fbs_name, "fcs": fcs_name, "fbs_comp": fc, "margin": margin_fbs,
                     "rank": poll.get((fcs_id, 2026)), "implied": implied, "week": g["week"],
                     "neutral": neutral, "score": f"{g['home_score']}-{g['away_score']}",
                     "home": hc_t["name"], "away": ac_t["name"]})

    print(f"FBS-vs-FCS fitted: {len(rows)}   (skipped, no live composite: {skipped})\n")
    if not rows:
        return 1

    def rep(sub, label):
        if not sub:
            print(f"  {label:<26} n=0")
            return None
        v = [r["implied"] for r in sub]
        m = sum(r["margin"] for r in sub) / len(sub)
        print(f"  {label:<26} n={len(sub):<4} implied FCS composite={sum(v)/len(v):>6.1f}  "
              f"(range {min(v):.0f}..{max(v):.0f})  mean FBS margin={m:>5.1f}")
        return sum(v) / len(v)

    print("IMPLIED FCS COMPOSITE (current scale, 50 = average FBS):")
    allc = rep(rows, "ALL FBS-vs-FCS")
    rank_avg = rep([r for r in rows if r["rank"] is not None], "vs poll-RANKED FCS")
    unrank_avg = rep([r for r in rows if r["rank"] is None], "vs UNRANKED FCS")

    print("\n  by FCS poll rank group:")
    g1 = [r for r in rows if r["rank"] is not None and r["rank"] <= 5]
    g2 = [r for r in rows if r["rank"] is not None and 6 <= r["rank"] <= 15]
    g3 = [r for r in rows if r["rank"] is not None and r["rank"] > 15]
    for sub, lab in ((g1, "rank 1-5"), (g2, "rank 6-15"), (g3, "rank 16-25")):
        rep(sub, lab)

    print("\n  CURRENT MODEL: composite 16 + 2.5 HFA + 15 boost")
    print(f"    vs an average FBS (comp 50) -> margin {(50-16)+2.5+15:.1f}")
    print(f"    measured mean margin 2026  -> {sum(r['margin'] for r in rows)/len(rows):.1f}")

    print("\nPROPOSED (boost removed):")
    if unrank_avg is not None and rank_avg is not None:
        print(f"    unranked FCS composite : {unrank_avg:.0f}")
        print(f"    ranked FCS composite   : {rank_avg:.0f}")
        print(f"    (gap {rank_avg - unrank_avg:.0f} pts — the differentiation the flat 16.0 cannot express)")
    if allc is not None:
        print(f"    single fallback value  : {allc:.0f}  (use when no Massey row exists)")

    # ── before / after on real games ────────────────────────────────────────
    C_UNRANK = unrank_avg if unrank_avg is not None else allc
    C_RANK = rank_avg if rank_avg is not None else allc
    if C_UNRANK is None or C_RANK is None:
        return 0

    def old_margin(r):
        return (r["fbs_comp"] - 16.0) + (0.0 if r["neutral"] else 2.5) + 15.0

    def new_margin(r):
        c = C_RANK if r["rank"] is not None else C_UNRANK
        return (r["fbs_comp"] - c) + (0.0 if r["neutral"] else 2.5)

    print("\n" + "=" * 84)
    print("BEFORE / AFTER on real 2026 games   (margin from the FBS team's view, +: FBS wins)")
    print("=" * 84)
    print(f"  {'wk':>2}  {'matchup':<40} {'fbsC':>5} {'OLD':>6} {'NEW':>6} {'ACTUAL':>7}")
    show = [r for r in rows if r["rank"] is not None][:14] + [r for r in rows if r["rank"] is None][:10]
    for r in sorted(show, key=lambda r: (r["week"], -r["fbs_comp"])):
        mu = f"{r['home']} {r['score']} {r['away']}"
        rk = f"#{r['rank']}" if r["rank"] is not None else "unr"
        print(f"  {r['week']:>2}  {mu[:40]:<40} {r['fbs_comp']:>5.0f} "
              f"{old_margin(r):>6.1f} {new_margin(r):>6.1f} {r['margin']:>7.0f}  [{rk}]")

    oe = [abs(old_margin(r) - r["margin"]) for r in rows]
    ne = [abs(new_margin(r) - r["margin"]) for r in rows]
    print(f"\n  mean |error| over all {len(rows)} games:")
    print(f"    OLD (comp 16 + 15 boost) : {sum(oe)/len(oe):>5.1f} pts")
    print(f"    NEW (fitted, no boost)   : {sum(ne)/len(ne):>5.1f} pts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())