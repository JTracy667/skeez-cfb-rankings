#!/usr/bin/env python3
"""backtest_v8.py — V8 work order: two new-input tests (matchup style, coaching).

BACKTEST ONLY. Nothing here touches the live app, its weights, or production (prod stays v28).

Base: V6.1 (slope 0.65, HFA 3.50), with base variant HFA 4.157 (bias-pinned).
A candidate survives only if it passes the full gate on BOTH bases:
  G1 interior optimum   G2 non-flat MAE surface   G3 no ATS degradation at the MAE-optimal weight

DATA NOTES (all verified before use — the work order's field names did not match the repo):
  * `off_epa_rush` does not exist; the field is `epa_rush` (offense) with `def_epa_rush` (defense).
  * `cfbd_analytics.json` carries coach_win_pct and EPA splits but is a CURRENT single snapshot
    with no season key, so it cannot be used on a 2021-25 backtest without leaking the present
    into the past. ARM 1's EPA splits are therefore rebuilt weekly from CFBD /plays via
    scripts/harvest_plays.py (prior weeks only), and ARM 2 uses /coaches per season via
    scripts/harvest_coaches.py, read as season Y-1 for a season-Y game (point in time).
  * trench uses lineYards from raw_stats_season_advanced_year{Y-1} (prior season, PIT).

    python scripts/backtest_v8.py --seasons 2021 2022 2023 2024 2025 --holdout 2025
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

os.environ.setdefault("REFRESH_INTERVAL_SECONDS", "0")

import backtest_v3 as v3          # noqa: E402
import backtest_v6 as v6          # noqa: E402
import backtest_v7 as v7          # noqa: E402

OUT = ROOT / "data" / "backtest_study"
CACHE = ROOT / "data" / "backtest_cache"

SLOPE = 0.65
HFA_MAE = 3.50
HFA_ZB = 4.157
GRID = [round(0.25 * i, 2) for i in range(17)]        # 0.00 .. 4.00 points per z-unit


def load_pbp(seasons):
    out = {}
    for s in seasons:
        p = ROOT / "data" / f"playbyplay_{s}.json"
        if p.exists():
            out[s] = json.loads(p.read_text(encoding="utf-8")).get("weeks", {})
    return out


def load_trench(seasons):
    """prior-season lineYards per team: {season: {team: (off_line, def_line)}}"""
    out = {}
    for s in seasons:
        p = CACHE / f"raw_stats_season_advanced_year{s - 1}.json"
        if not p.exists():
            continue
        m = {}
        for r in json.loads(p.read_text(encoding="utf-8")):
            off = r.get("offense") or {}
            dfn = r.get("defense") or {}
            if isinstance(off.get("lineYards"), (int, float)) and isinstance(dfn.get("lineYards"), (int, float)):
                m[r["team"]] = (off["lineYards"], dfn["lineYards"])
        out[s] = m
    return out


def load_coaches(seasons):
    """prior-season head-coach win pct per school: {season: {school: win_pct}}"""
    out = {}
    for s in seasons:
        p = CACHE / f"coaches_{s - 1}.json"
        if p.exists():
            out[s] = {k: v["win_pct"] for k, v in json.loads(p.read_text(encoding="utf-8")).items()
                      if isinstance(v.get("win_pct"), (int, float))}
    return out


def pbp_prior(pbp, season, week, team, keys):
    """mean of each key over weeks strictly BEFORE `week` (PIT)"""
    acc = {k: [] for k in keys}
    for wk, teams in (pbp.get(season) or {}).items():
        if int(wk) >= week:
            continue
        m = (teams or {}).get(team)
        if not m:
            continue
        for k in keys:
            if isinstance(m.get(k), (int, float)):
                acc[k].append(m[k])
    return {k: (sum(v) / len(v) if v else None) for k, v in acc.items()}


def build_features(rows, seasons):
    pbp = load_pbp(seasons)
    trench = load_trench(seasons)
    coaches = load_coaches(seasons)
    for r in rows:
        s, wk, h, a = r["season"], r["week"], r["home"], r["away"]
        keys = ("epa_rush", "def_epa_rush", "epa_pass", "def_epa_pass")
        ph, pa = pbp_prior(pbp, s, wk, h, keys), pbp_prior(pbp, s, wk, a, keys)
        ok = all(ph[k] is not None and pa[k] is not None for k in keys)
        r["_rush_matchup"] = (((ph["epa_rush"] - pa["def_epa_rush"])
                               - (pa["epa_rush"] - ph["def_epa_rush"])) if ok else None)
        r["_pass_matchup"] = (((ph["epa_pass"] - pa["def_epa_pass"])
                               - (pa["epa_pass"] - ph["def_epa_pass"])) if ok else None)
        tm = trench.get(s) or {}
        if h in tm and a in tm:
            (oh, dh), (oa, da) = tm[h], tm[a]
            r["_trench_matchup"] = (oh - da) - (oa - dh)
        else:
            r["_trench_matchup"] = None
        cm = coaches.get(s) or {}
        r["_coach_diff"] = (cm[h] - cm[a]) if (h in cm and a in cm) else None


def zscores(vals, sel):
    arr = np.array([np.nan if v is None else v for v in vals], float)
    mu = np.nanmean(arr[sel])
    sd = np.nanstd(arr[sel])
    z = (arr - mu) / sd if sd else arr - mu
    return z, mu, sd


def run_arm(rows, idx, nl, mask, book, sea, seasons, holdout, name, feats, report):
    """Grid a single combined add-on term across both bases; report metrics + gates."""
    fit_s = [s for s in seasons if s != holdout]
    sel = np.isin(sea, fit_s)
    zs = []
    for f in feats:
        z, mu, sd = zscores([r.get(f) for r in rows], sel)
        zs.append(z)
        report.setdefault("feature_stats", {})[f] = {"mean": round(float(mu), 5),
                                                     "sd": round(float(sd), 5),
                                                     "present": int(np.sum(~np.isnan(z)))}
    combo = np.nanmean(np.vstack(zs), axis=0)
    # STRICT: a row counts only if EVERY component feature is present. nanmean would silently
    # fold a row with one feature into the "combined" term as if it were the whole composite.
    keep = ~np.isnan(np.vstack(zs)).any(axis=0)
    print(f"[v8] {name}: {int(keep.sum())} of {len(rows)} games have all inputs "
          f"({', '.join(feats)})")
    if keep.sum() < 200:
        print(f"[v8] {name}: SKIPPED — too few games")
        return None

    out = {}
    for label, hfa in (("hfa3.50", HFA_MAE), ("hfa4.157", HFA_ZB)):
        base = np.array([nl[i] * SLOPE + hfa * mask[i] for i in range(len(rows))])
        b = book[keep]
        f = np.isin(sea[keep], fit_s)
        bk = base[keep]
        surf = []
        for c in GRID:
            mm = bk + c * combo[keep]
            surf.append({"c": c, "fit_mae": round(float(np.abs(mm[f] - b[f]).mean()), 4)})
        best = min(surf, key=lambda x: x["fit_mae"])
        c = best["c"]
        interior = c not in (GRID[0], GRID[-1])
        nb = {x["c"]: x["fit_mae"] for x in surf}
        lo_n, hi_n = nb.get(round(c - 0.25, 2)), nb.get(round(c + 0.25, 2))
        nonflat = lo_n is not None and hi_n is not None and best["fit_mae"] < lo_n and best["fit_mae"] < hi_n
        swing = max(x["fit_mae"] for x in surf) - min(x["fit_mae"] for x in surf)

        # ATS at the candidate vs the same subset's base
        sub = [rows[i] for i in np.where(keep)[0]]
        for r in sub:
            r["model_margin"] = float(nl[idx[id(r)]] * SLOPE + hfa * mask[idx[id(r)]])
        b_ats = v3.fmt(v3.ev(sub, None))["ats_fbs_pct"]
        for r in sub:
            r["model_margin"] = float(nl[idx[id(r)]] * SLOPE + hfa * mask[idx[id(r)]]
                                      + c * combo[idx[id(r)]])
        p = v3.fmt(v3.ev(sub, None))
        hold = v3.fmt(v3.ev(sub, [holdout]))
        per = {str(s): v3.fmt(v3.ev(sub, [s]))["ats_fbs_pct"] for s in seasons}
        vals = [x for x in per.values() if x is not None]
        band = round(max(vals) - min(vals), 2) if vals else None
        g3 = p["ats_fbs_pct"] is not None and b_ats is not None and p["ats_fbs_pct"] >= b_ats
        passed = bool(interior and nonflat and g3)
        print(f"[v8] {name} [{label}] best c={c} (fit MAE {best['fit_mae']:.4f}, swing {swing:.4f}) "
              f"ATS base {b_ats}% -> {p['ats_fbs_pct']}%  MAE {p['mae_vs_book']}  bias "
              f"{p['bias_vs_book']}  band {band}pp  held-out {hold['ats_fbs_pct']}%  "
              f"G1={interior} G2={nonflat} G3={g3} -> {'PASS' if passed else 'FAIL'}")
        out[label] = {"weight_per_unit": c, "fit_mae": best["fit_mae"], "surface_swing": round(swing, 4),
                      "base_ats": b_ats, "pooled": p, "holdout": hold, "per_season": per,
                      "band": band, "G1_interior": interior, "G2_nonflat": nonflat,
                      "G3_no_ats_drop": g3, "PASS": passed, "grid": surf}
    verdict = all(v["PASS"] for v in out.values())
    report[name] = {"features": feats, "coverage": int(keep.sum()), "bases": out,
                    "VERDICT": "PASS" if verdict else "FAIL"}
    print(f"[v8] {name}: VERDICT {'PASS' if verdict else 'FAIL'} "
          f"(must pass on both bases)")
    return verdict


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", type=int, nargs="+", default=[2021, 2022, 2023, 2024, 2025])
    ap.add_argument("--holdout", type=int, default=2025)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    recs = v3.load(args.seasons)
    rows, nl, mask, book, act, sea = v7.build(recs, args.seasons, args.holdout)
    idx = {id(r): i for i, r in enumerate(rows)}
    print(f"[v8] base V6.1 slope {SLOPE} hfa {HFA_MAE} (variant {HFA_ZB}); {len(rows)} games")
    build_features(rows, args.seasons)

    report = {"seasons": args.seasons, "holdout": args.holdout, "slope": SLOPE,
              "hfa_bases": {"mae": HFA_MAE, "zero_bias": HFA_ZB}, "grid": [GRID[0], GRID[-1]]}

    # collinearity among the three matchup features (does the combined term double-count?)
    trio = np.array([[r["_rush_matchup"] or np.nan for r in rows],
                     [r["_pass_matchup"] or np.nan for r in rows],
                     [r["_trench_matchup"] or np.nan for r in rows]], float)
    ok = ~np.isnan(trio).any(axis=0)
    if ok.sum() > 50:
        cm = np.corrcoef(trio[:, ok])
        print(f"[v8] matchup-feature correlations (n={int(ok.sum())}): "
              f"rush~pass {cm[0,1]:+.3f}  rush~trench {cm[0,2]:+.3f}  pass~trench {cm[1,2]:+.3f}")
        report["matchup_correlations"] = {"rush_pass": round(float(cm[0, 1]), 3),
                                          "rush_trench": round(float(cm[0, 2]), 3),
                                          "pass_trench": round(float(cm[1, 2]), 3),
                                          "n": int(ok.sum())}

    passed = run_arm(rows, idx, nl, mask, book, sea, args.seasons, args.holdout,
                     "arm1_matchup_combined",
                     ["_rush_matchup", "_pass_matchup", "_trench_matchup"], report)
    if passed:
        print("[v8] arm1 combined PASSED — splitting into 3 as the work order directs")
        for f in ("_rush_matchup", "_pass_matchup", "_trench_matchup"):
            run_arm(rows, idx, nl, mask, book, sea, args.seasons, args.holdout,
                    f"arm1_split{f}", [f], report)
    else:
        print("[v8] arm1 combined did NOT pass — not splitting (per the work order)")

    run_arm(rows, idx, nl, mask, book, sea, args.seasons, args.holdout,
            "arm2_coach_win_pct", ["_coach_diff"], report)

    dest = Path(args.out) if args.out else OUT / f"v8_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    dest.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"[v8] report -> {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())