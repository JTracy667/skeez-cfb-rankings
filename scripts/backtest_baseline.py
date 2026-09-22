#!/usr/bin/env python3
"""backtest_baseline.py — per-stat predictive scores on 2021-2025 (Phase 5.2).

WHAT THIS IS
For every team-level stat we stored, measure how well the game differential
(home_stat - away_stat) predicts the actual margin (home_score - away_score):

    corr        Pearson correlation of differential vs actual margin
    sign_acc    share of games where the differential picked the winner
    n           games where both teams had that stat

WHAT THIS IS NOT — read before quoting a number
The ingested stats are SEASON AGGREGATES (one row per team-season, week 0). They
therefore INCLUDE the games being scored, so these are not leakage-free
out-of-sample scores: they measure how strongly a stat's season differential
tracks margin, which is a RANKING OF SIGNAL, not a predictive performance claim.
A true walk-forward backtest needs WEEKLY stat pulls (CFBD serves them; ingesting
them is ~84 calls for 6 seasons x 14 weeks — cheap, not yet done). The report says
this in its own words so nobody quotes a leaked number as predictive.

Usage:  python scripts/backtest_baseline.py [--out D1_BACKTEST_REPORT.md]
        python scripts/backtest_baseline.py --seasons 2021,2022,2023,2024,2025
"""
from __future__ import annotations

import argparse
import math
import os
import sqlite3
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 30:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "d1_export.sqlite"))
    ap.add_argument("--out", default=os.path.join(ROOT, "D1_BACKTEST_REPORT.md"))
    ap.add_argument("--seasons", default="2021,2022,2023,2024,2025")
    ap.add_argument("--min-games", type=int, default=200)
    args = ap.parse_args()

    seasons = [int(s) for s in args.seasons.split(",") if s.strip()]
    if not os.path.exists(args.db):
        print(f"no export at {args.db} — run scripts/export_d1.py first")
        return 1

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    ph = ",".join("?" * len(seasons))

    games = con.execute(
        f"SELECT game_id, season, week, home_id, away_id, home_score, away_score "
        f"FROM games WHERE season IN ({ph}) "
        f"AND home_score IS NOT NULL AND away_score IS NOT NULL",
        seasons).fetchall()
    print(f"games (2021-2025, scored): {len(games)}")

    stats = con.execute(
        f"SELECT stat_key, subject_id, season, value FROM stat_observations "
        f"WHERE subject_type='team' AND season IN ({ph})", seasons).fetchall()
    print(f"team-stat rows           : {len(stats)}")

    # (stat_key, season, team_id) -> value  and  (stat_key) -> set of seasons seen
    by_stat_team: dict[tuple, float] = {}
    keys: set[str] = set()
    for r in stats:
        try:
            v = float(r["value"])
        except (TypeError, ValueError):
            continue
        by_stat_team[(r["stat_key"], r["season"], r["subject_id"])] = v
        keys.add(r["stat_key"])
    print(f"stat keys                : {len(keys)}")

    results = []
    for key in sorted(keys):
        xs: list[float] = []
        ys: list[float] = []
        hits = 0
        for g in games:
            h = by_stat_team.get((key, g["season"], g["home_id"]))
            a = by_stat_team.get((key, g["season"], g["away_id"]))
            if h is None or a is None:
                continue
            diff = h - a
            margin = float(g["home_score"]) - float(g["away_score"])
            if margin == 0:
                continue
            xs.append(diff)
            ys.append(margin)
            if (diff > 0) == (margin > 0):
                hits += 1
        n = len(xs)
        if n < args.min_games:
            continue
        corr = pearson(xs, ys)
        results.append({
            "key": key, "n": n,
            "corr": corr if corr is not None else float("nan"),
            "sign_acc": hits / n if n else 0.0,
        })

    results.sort(key=lambda r: (r["corr"] if not math.isnan(r["corr"]) else -9), reverse=True)

    print(f"\n{'stat_key':28} {'n':>6} {'corr':>8} {'sign_acc':>9}")
    for r in results:
        print(f"{r['key']:28} {r['n']:>6} {r['corr']:>8.3f} {r['sign_acc']*100:>8.1f}%")

    strong = [r for r in results if r["corr"] >= 0.5]
    print(f"\nstats with corr >= 0.5: {len(strong)}")
    print("top 5:", ", ".join(f"{r['key']} ({r['corr']:.2f})" for r in results[:5]))

    now = datetime.now(timezone.utc)
    lines = [
        "# D1 baseline backtest — per-stat predictive scores (2021-2025)",
        "",
        f"Generated {now.strftime('%Y-%m-%d %H:%MZ')} by `scripts/backtest_baseline.py`",
        f"Source: local export of D1 (`data/d1_export.sqlite`), {len(games)} scored games, "
        f"{len(keys)} team-level stats.",
        "",
        "## Read this before quoting any number",
        "",
        "**These are NOT leakage-free out-of-sample scores.** The ingested stats are",
        "**season aggregates** (one row per team-season, `week` 0), so each stat value",
        "already includes the games it is being scored against. What this table measures",
        "is how strongly a stat's *season differential* tracks the final margin — a",
        "**ranking of statistical signal**, not a claim about predictive performance.",
        "",
        "A true walk-forward backtest needs **weekly** stat pulls (CFBD serves them;",
        "~84 calls for 6 seasons x 14 weeks). Until those are ingested, treat the",
        "ordering below as a shortlist of what is worth testing properly, and nothing",
        "more. Method: differential = home_stat - away_stat; corr vs actual margin;",
        "`sign_acc` = share of games the differential picked the winner.",
        "",
        "## Results",
        "",
        "| stat_key | games | corr | sign_acc |",
        "|---|---:|---:|---:|",
    ]
    for r in results:
        lines.append(f"| `{r['key']}` | {r['n']} | {r['corr']:.3f} | {r['sign_acc']*100:.1f}% |")
    lines += [
        "",
        "## Blocked / honest limits",
        "",
        "* No weekly granularity in `stat_observations` yet -> no leakage-free backtest.",
        "* Historical line *movement* exists only from 2026 forward (risk register C1):",
        "  CLV-style backtests before that are closing-line-only.",
        "* FCS-specific stats remain unavailable from CFBD (silently ignores",
        "  `division=fcs`); `fcs_rating` (Coaches Poll) is the only FCS signal stored.",
        "",
    ]
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print(f"\nreport -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())