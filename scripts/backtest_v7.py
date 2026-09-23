#!/usr/bin/env python3
"""backtest_v7.py — V7 work order: refit the V6 base scale, then ONE gated add-on.

BACKTEST ONLY. Nothing here touches the live app, its weights, or production (prod stays v28).

STEP 1 — V6.1: refit scale INSIDE the V6 formula family
    Formula:  margin = copysign(|D|^1.1, D) * slope + HFA * (1 if home else 0)
              D = V6 composite gap = 0.185567*SP+ + 0.309278*EFF + 0.319588*TAL + 0.185567*EXP
    Refit slope (0.10 .. 1.50, step 0.05) and HFA (2.00 .. 5.00, step 0.25) by MAE, jointly.
    Objective = MAE vs the book line, which is the SAME objective V3/V4's slopes were fitted
    with (backtest_study.evaluate: abs_err = |model_margin - book_margin|). Fitting to the line
    is what drives bias to zero, so "bias near 0" is a check on the fit, not a free win.
    V6.1 becomes the reference arm for all future add-ons.

STEP 2 — add-on: offensive EPA (play-by-play, prior weeks only)
    d_off_epa = home minus away mean ppa (CFBD's per-play EPA) over weeks strictly BEFORE the
    game. 0-20 grid first; if the optimum pins to the grid edge, re-run 0-80 and report the
    surface. STABILITY GATE — all three must pass to survive:
      G1 interior optimum (not at either grid edge)
      G2 non-flat MAE surface (optimum strictly better than both adjacent grid points)
      G3 no ATS degradation at the MAE-optimal weight (vs the base on the same subset)

Report: ATS + O/U + SU, per-season band, held-out 2025, MAE/bias, fitted slope + HFA.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

os.environ.setdefault("REFRESH_INTERVAL_SECONDS", "0")   # keep the live scheduler off

import backtest_v3 as v3          # noqa: E402
import backtest_v6 as v6          # noqa: E402

OUT = ROOT / "data" / "backtest_study"

SLOPE_GRID = [round(0.10 + 0.05 * i, 2) for i in range(29)]     # 0.10 .. 1.50
HFA_GRID = [round(2.00 + 0.25 * i, 2) for i in range(13)]       # 2.00 .. 5.00
POWER = 1.1                                                     # V6 family power


def build(recs, seasons, holdout):
    """Vectorise: D (composite gap), nl (non-linear term), neutral mask, book margin."""
    rows, D, mask, book, act, sea = [], [], [], [], [], []
    for r in recs:
        d = v6.v6_composite(r)
        if d is None or r.get("spread_close") is None:
            continue
        rows.append(r)
        D.append(d)
        mask.append(0.0 if r.get("neutral") else 1.0)
        book.append(-r["spread_close"])
        act.append(r["home_score"] - r["away_score"])
        sea.append(r["season"])
    D = np.array(D, float)
    nl = np.copysign(np.abs(D) ** POWER, D)
    return rows, nl, np.array(mask), np.array(book), np.array(act), np.array(sea)


def fit_surface(nl, mask, book, sea, fit_seasons):
    """Joint (slope, HFA) grid by MAE vs book on the fit seasons. Returns best + full surface."""
    sel = np.isin(sea, fit_seasons)
    surf = {}
    for s in SLOPE_GRID:
        for h in HFA_GRID:
            mm = nl * s + h * mask
            surf[(s, h)] = float(np.abs(mm[sel] - book[sel]).mean())
    (bs, bh), bmae = min(surf.items(), key=lambda kv: kv[1])
    return (bs, bh), bmae, surf


def zero_bias_hfa(nl, mask, book, sel, slope, lo=-5.0, hi=15.0, tol=1e-4):
    """Smallest |bias| HFA at a fixed slope, by bisection. MAE-optimal HFA need NOT zero the
    bias (MAE drives the MEDIAN residual to zero, not the mean), so this is a separate,
    explicit choice with its own MAE cost — which is reported, not hidden."""
    f = lambda h: float((nl * slope + h * mask)[sel].mean() - book[sel].mean())  # noqa: E731
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if f(mid) < 0:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    h = (lo + hi) / 2.0
    return round(h, 3)


def evaluate(rows, margin_fn, seasons, holdout, report, key, extra=""):
    for r in rows:
        m = margin_fn(r)
        if m is not None:
            r["model_margin"] = m
    pooled = v3.fmt(v3.ev(rows, None))
    hold = v3.fmt(v3.ev(rows, [holdout]))
    per = {str(s): v3.fmt(v3.ev(rows, [s]))["ats_fbs_pct"] for s in seasons}
    vals = [v for v in per.values() if v is not None]
    band = round(max(vals) - min(vals), 2) if vals else None
    print(f"[v7] {key:<28} ATS {pooled['ats_fbs_pct']}% ({pooled['ats_fbs']}, {pooled['fbs_games']})  "
          f"O/U {pooled['ou_pct']}%  SU {pooled['su_pct']}%  MAE {pooled['mae_vs_book']}  "
          f"|m| {pooled['mean_model_margin']} vs {pooled['mean_book_line']}  "
          f"bias {pooled['bias_vs_book']}  band {band}pp  held-out {hold['ats_fbs_pct']}%"
          f"{('  ' + extra) if extra else ''}")
    res = {"pooled": pooled, "holdout": hold, "per_season": per, "band": band, "note": extra}
    report[key] = res
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description="V7: V6.1 scale refit + gated offensive-EPA add-on")
    ap.add_argument("--seasons", type=int, nargs="+", default=[2021, 2022, 2023, 2024, 2025])
    ap.add_argument("--holdout", type=int, default=2025)
    ap.add_argument("--epa-max", type=float, default=20.0)
    ap.add_argument("--epa-step", type=float, default=1.0)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    recs = v3.load(args.seasons)
    fit_s = [s for s in args.seasons if s != args.holdout]
    report = {"seasons": args.seasons, "holdout": args.holdout,
              "weights": v6.V6_W, "power": POWER,
              "slope_grid": [SLOPE_GRID[0], SLOPE_GRID[-1]], "hfa_grid": [HFA_GRID[0], HFA_GRID[-1]]}
    rows, nl, mask, book, act, sea = build(recs, args.seasons, args.holdout)
    idx = {id(r): i for i, r in enumerate(rows)}
    print(f"[v7] {len(rows)} games with a computable V6 gap and a book line (fit {fit_s})")

    # ---------------- STEP 1: refit slope + HFA ----------------
    (bs, bh), bmae, surf = fit_surface(nl, mask, book, sea, fit_s)
    flat = max(surf.values()) - min(surf.values())
    print(f"[v7] STEP1 fitted slope {bs}  HFA {bh}  (fit MAE vs book {bmae:.4f})")
    print(f"[v7] STEP1 grid surface: MAE spans {min(surf.values()):.4f} .. {max(surf.values()):.4f} "
          f"(full swing {flat:.4f})")
    s_lo = surf.get((round(bs - 0.05, 2), bh))
    s_hi = surf.get((round(bs + 0.05, 2), bh))
    h_lo = surf.get((bs, round(bh - 0.25, 2)))
    h_hi = surf.get((bs, round(bh + 0.25, 2)))
    print(f"[v7] STEP1 neighbours: slope- {s_lo} slope+ {s_hi} | hfa- {h_lo} hfa+ {h_hi}"
          f"{'  [SLOPE AT EDGE]' if bs in (SLOPE_GRID[0], SLOPE_GRID[-1]) else ''}"
          f"{'  [HFA AT EDGE]' if bh in (HFA_GRID[0], HFA_GRID[-1]) else ''}")
    top = sorted(surf.items(), key=lambda kv: kv[1])[:5]
    print("[v7] STEP1 best 5 grid points: " + ", ".join(f"({s},{h})={m:.4f}" for (s, h), m in top))

    def v61(r, addon=0.0):
        i = idx[id(r)]
        return float(nl[i] * bs + bh * mask[i] + addon)

    base = evaluate(rows, lambda r, i=idx: v61(r), args.seasons, args.holdout, report, "v6.1_base")
    base["slope"], base["hfa"] = bs, bh
    base["fit_mae_vs_book"] = round(bmae, 4)
    base["grid_surface_span"] = round(flat, 4)
    base["neighbours"] = {"slope_minus": s_lo, "slope_plus": s_hi, "hfa_minus": h_lo, "hfa_plus": h_hi}

    # --- zero-bias variant: the work order asks to CONFIRM bias near 0; the MAE optimum does
    # not deliver that (MAE centres the median residual, not the mean), so solve it explicitly.
    sel_fit = np.isin(sea, fit_s)
    hzb = zero_bias_hfa(nl, mask, book, sel_fit, bs)
    fit_bias_opt = float(((nl * bs + bh * mask)[sel_fit] - book[sel_fit]).mean())
    fit_bias_zb = float(((nl * bs + hzb * mask)[sel_fit] - book[sel_fit]).mean())
    mae_zb = float(np.abs((nl * bs + hzb * mask)[sel_fit] - book[sel_fit]).mean())
    print(f"[v7] STEP1 fit-season bias: MAE-optimal HFA {bh} -> {fit_bias_opt:+.3f} | "
          f"zero-bias HFA {hzb} -> {fit_bias_zb:+.3f} (MAE {bmae:.4f} -> {mae_zb:.4f}, "
          f"cost {mae_zb - bmae:+.4f})")

    def v61zb(r):
        i = idx[id(r)]
        return float(nl[i] * bs + hzb * mask[i])

    zb = evaluate(rows, lambda r: v61zb(r), args.seasons, args.holdout, report,
                  "v6.1_base_zerobias", extra=f"slope {bs} hfa {hzb} (bias-pinned)")
    zb["slope"], zb["hfa"] = bs, hzb
    zb["fit_mae_vs_book"] = round(mae_zb, 4)
    zb["fit_bias"] = round(fit_bias_zb, 4)
    base["fit_bias"] = round(fit_bias_opt, 4)

    # V6 (old, unrefit) on the same rows, for the before/after
    for r in rows:
        r["model_margin"] = v6.v6_margin(r)
    evaluate(rows, lambda r: r["model_margin"], args.seasons, args.holdout, report,
             "v6_0_before_refit", extra="slope 0.6378 hfa 3.55, same rows")

    # MAE vs ACTUAL margin (honesty check: MAE vs book is a tracking metric, not accuracy)
    sel_fit = np.isin(sea, fit_s)
    mm = nl * bs + bh * mask
    print(f"[v7] STEP1 MAE vs ACTUAL margin: fit {np.abs(mm[sel_fit] - act[sel_fit]).mean():.3f}  "
          f"holdout {np.abs(mm[~sel_fit] - act[~sel_fit]).mean():.3f}")
    for r in rows:
        r["model_total"] = r.get("model_total")

    # ---------------- STEP 2: offensive EPA add-on ----------------
    pbp = {}
    for s in args.seasons:
        p = ROOT / "data" / f"playbyplay_{s}.json"
        if p.exists():
            pbp[s] = json.loads(p.read_text(encoding="utf-8")).get("weeks", {})
    if not pbp:
        print("[v7] STEP2 skipped — no play-by-play cache")
    else:
        sub = []
        for r in rows:
            def prior(team):
                acc = []
                for wk, teams in (pbp.get(r["season"]) or {}).items():
                    if int(wk) >= r["week"]:
                        continue
                    m = (teams or {}).get(team)
                    if m and isinstance(m.get("off_ppa"), (int, float)):
                        acc.append(m["off_ppa"])
                return sum(acc) / len(acc) if acc else None
            ph, pa = prior(r["home"]), prior(r["away"])
            if ph is None or pa is None:
                continue
            r["_d_epa"] = ph - pa
            sub.append(r)
        print(f"[v7] STEP2 coverage: {len(sub)} games with prior-week offensive EPA for BOTH teams")
        mag = sorted(abs(r["_d_epa"]) for r in sub)
        med, p90 = mag[len(mag) // 2], mag[9 * len(mag) // 10]
        print(f"[v7] |d_off_epa| median {med:.4f}  p90 {p90:.4f}")

        def run_grid(lo, hi, step, hh):
            grid_vals = [round(lo + step * i, 2) for i in range(int(round((hi - lo) / step)) + 1)]
            bk = np.array([-r["spread_close"] for r in sub])
            st = np.array([r["season"] for r in sub])
            f = np.isin(st, fit_s)
            b = np.array([float(nl[idx[id(r)]] * bs + hh * mask[idx[id(r)]]) for r in sub])
            e = np.array([r["_d_epa"] for r in sub])
            out = []
            for c in grid_vals:
                mm_ = b + c * e
                out.append({"c": c, "fit_mae": round(float(np.abs(mm_[f] - bk[f]).mean()), 4),
                            "pooled_mae": round(float(np.abs(mm_ - bk).mean()), 4)})
            return grid_vals, out, min(out, key=lambda x: x["fit_mae"])

        for label, hh in (("v6.1", bh), ("v6.1zb", hzb)):
            def vsub(r, hh=hh):
                i = idx[id(r)]
                return float(nl[i] * bs + hh * mask[i])

            bkey = f"step2_base_subset_{label}"
            evaluate(sub, vsub, args.seasons, args.holdout, report, bkey,
                     extra=f"control on the add-on's coverage (hfa {hh})")
            pinned = False
            for span, step in ((args.epa_max, args.epa_step), (80.0, 2.0), (200.0, 5.0)):
                if span != args.epa_max and not pinned:
                    break
                gv, surface, best = run_grid(0.0, span, step, hh)
                c = best["c"]
                interior = c not in (gv[0], gv[-1])
                pinned = not interior
                nb = {x["c"]: x["fit_mae"] for x in surface}
                lo_n = nb.get(round(c - step, 2))
                hi_n = nb.get(round(c + step, 2))
                nonflat = (lo_n is not None and hi_n is not None
                           and best["fit_mae"] < lo_n and best["fit_mae"] < hi_n)
                swing = max(x["fit_mae"] for x in surface) - min(x["fit_mae"] for x in surface)
                print(f"[v7] STEP2 [{label}] grid 0-{span:g} step {step:g}: best c={c} "
                      f"fit MAE {best['fit_mae']:.4f}  surface swing {swing:.4f}  "
                      f"interior={interior}  non-flat={nonflat}"
                      f"{'  [EDGE]' if not interior else ''}")
                for r in sub:
                    r["model_margin"] = vsub(r) + c * r["_d_epa"]
                res = evaluate(sub, lambda r: r["model_margin"], args.seasons, args.holdout, report,
                               f"step2_off_epa_{label}_c{c}",
                               extra=f"typical add-on {c * med:+.2f} pts, p90 {c * p90:+.2f}")
                b_ats = report[bkey]["pooled"]["ats_fbs_pct"]
                res["weight_per_unit"] = c
                res["base"] = {"label": label, "slope": bs, "hfa": hh}
                res["grid_span"] = span
                res["grid_surface_swing"] = round(swing, 4)
                res["gate_G1_interior"] = interior
                res["gate_G2_nonflat"] = nonflat
                res["gate_G3_no_ats_degradation"] = (
                    res["pooled"]["ats_fbs_pct"] is not None and b_ats is not None
                    and res["pooled"]["ats_fbs_pct"] >= b_ats)
                res["gate_PASS"] = bool(res["gate_G1_interior"] and res["gate_G2_nonflat"]
                                        and res["gate_G3_no_ats_degradation"])
                res["median_abs_contribution_pts"] = round(c * med, 3)
                res["grid"] = surface
                print(f"[v7] STEP2 [{label}] GATES c={c}: interior={interior} "
                      f"non-flat={nonflat} no-ATS-degradation={res['gate_G3_no_ats_degradation']} "
                      f"-> {'PASS' if res['gate_PASS'] else 'FAIL'}")
                report[f"step2_grid_{label}_0_{int(span)}"] = {
                    "best_c": c, "swing": round(swing, 4), "interior": interior,
                    "nonflat": nonflat, "surface": surface}

    dest = Path(args.out) if args.out else OUT / f"v7_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    dest.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"[v7] report -> {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())