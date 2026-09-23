#!/usr/bin/env python3
"""backtest_v6.py — V6 work order: lock the new base model, then test ONE add-on.

BACKTEST ONLY. Nothing here touches the live app, its weights, or production.

STEP 1 — V6 BASE
    Weights (Jeff): SP+ 0.18, efficiency 0.30, talent 0.31, experience 0.18.
    *** Those four sum to 0.97, not 1.0 *** — they are the V5 standardized shares, and the
    missing 0.03 was the dead Level 2 block that V5 retired. A composite that does not total
    1.0 is silently mis-calibrated, so the four are renormalized (x 1/0.97) and the exact
    numbers used are printed. Rejected alternative: running 0.97 as given and reporting a
    number that is scaled wrong by 3%.
    HFA 3.55 · calibration power 1.1, slope 0.6378 · Level 2 empty.
    A control arm (the V5 OLS regression itself) runs alongside so the base can be checked
    against the regression it was derived from.

STEP 2 — ADD-ON #1: explosiveness (play-by-play, weekly, prior weeks only)
    d_explosive_rate = home minus away weekly explosive-play rate (20+ yd), averaged over
    weeks strictly before the game. Added to the V6 margin as c * d. Weight c gridded; the
    MAE-optimal c is reported, plus the implied margin contribution so the "weight" can be
    read either way (per-unit coefficient or typical points).

Report: ATS + O/U + SU, per-season band, held-out 2025, MAE/bias vs book, mean|margin| vs book.

    python scripts/backtest_v6.py --seasons 2021 2022 2023 2024 2025 --holdout 2025
    python scripts/backtest_v6.py ... --no-neutral-hfa     # flat 3.55 on neutral sites too
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

# Keep the live refresh scheduler (started on `import app`) from spending real API credits.
os.environ.setdefault("REFRESH_INTERVAL_SECONDS", "0")

import backtest_v3 as v3  # noqa: E402

OUT = ROOT / "data" / "backtest_study"

# ---- STEP 1 constants ---------------------------------------------------------------
W_AS_GIVEN = {"sp": 0.18, "eff": 0.30, "tal": 0.31, "exp": 0.18}
assert abs(sum(W_AS_GIVEN.values()) - 0.97) < 1e-9, "work-order weights changed?"
_scale = 1.0 / sum(W_AS_GIVEN.values())
V6_W = {k: round(v * _scale, 6) for k, v in W_AS_GIVEN.items()}
assert abs(sum(V6_W.values()) - 1.0) < 1e-6, "renormalized weights must total 1.0"

V6_HFA = 3.55
V6_POWER = 1.1
V6_SLOPE = 0.6378

EXPL_GRID = [round(0.0 + 1.0 * i, 2) for i in range(21)]   # 0..20 points per unit of rate diff


def v6_composite(r: dict) -> float | None:
    """V6 gap: renormalized regression shares over SP+, efficiency, talent, experience."""
    need = ("sp_h", "sp_a", "eff_h", "eff_a", "tal_h", "tal_a")
    if any(r.get(k) is None for k in need):
        return None
    exp_h = r.get("exp_h")
    exp_a = r.get("exp_a")
    exp_h = 50.0 if exp_h is None else exp_h       # V2 convention: neutral 50 when unknown
    exp_a = 50.0 if exp_a is None else exp_a
    return (V6_W["sp"] * (r["sp_h"] - r["sp_a"])
            + V6_W["eff"] * (r["eff_h"] - r["eff_a"])
            + V6_W["tal"] * (r["tal_h"] - r["tal_a"])
            + V6_W["exp"] * (exp_h - exp_a))


def v6_margin(r: dict, flat_hfa: bool = False, addon: float = 0.0) -> float | None:
    d = v6_composite(r)
    if d is None:
        return None
    nl = math.copysign(abs(d) ** V6_POWER, d) * V6_SLOPE
    hfa = V6_HFA if (flat_hfa or not r.get("neutral")) else 0.0
    return nl + hfa + addon


def band_of(per: dict) -> float | None:
    vals = [v for v in per.values() if v is not None]
    return round(max(vals) - min(vals), 2) if vals else None


def block(label, recs, seasons, holdout, report, key, extra="") -> dict:
    pooled = v3.fmt(v3.ev(recs, None))
    hold = v3.fmt(v3.ev(recs, [holdout]))
    per = {str(s): v3.fmt(v3.ev(recs, [s]))["ats_fbs_pct"] for s in seasons}
    b = band_of(per)
    print(f"[v6] {label:<30} ATS {pooled['ats_fbs_pct']}% ({pooled['ats_fbs']}, {pooled['fbs_games']})  "
          f"O/U {pooled['ou_pct']}%  SU {pooled['su_pct']}%  MAE {pooled['mae_vs_book']}  "
          f"|m| {pooled['mean_model_margin']} vs {pooled['mean_book_line']}  "
          f"bias {pooled['bias_vs_book']}  band {b}pp  held-out {hold['ats_fbs_pct']}%"
          f"{('  ' + extra) if extra else ''}")
    res = {"pooled": pooled, "holdout": hold, "per_season": per, "band": b, "note": extra}
    report[key] = res
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description="V6 base + add-on #1 (backtest only)")
    ap.add_argument("--seasons", type=int, nargs="+", default=[2021, 2022, 2023, 2024, 2025])
    ap.add_argument("--holdout", type=int, default=2025)
    ap.add_argument("--no-neutral-hfa", action="store_true",
                    help="apply HFA 3.55 flat, including neutral-site games")
    ap.add_argument("--expl-max", type=float, default=20.0,
                    help="grid ceiling for the explosiveness weight (points per unit rate diff)")
    ap.add_argument("--expl-step", type=float, default=1.0)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    print(f"[v6] V6 weights as given {W_AS_GIVEN} (sum {sum(W_AS_GIVEN.values()):.3f})")
    print(f"[v6] RENORMALIZED x{_scale:.6f} -> {V6_W} (sum {sum(V6_W.values()):.3f})")
    print(f"[v6] HFA {V6_HFA}  power {V6_POWER}  slope {V6_SLOPE}  Level 2: empty")

    recs = v3.load(args.seasons)
    print(f"[v6] {len(recs)} records")
    fit_s = [s for s in args.seasons if s != args.holdout]

    report = {"seasons": args.seasons, "holdout": args.holdout,
              "weights_as_given": W_AS_GIVEN, "weights_used": V6_W,
              "hfa": V6_HFA, "power": V6_POWER, "slope": V6_SLOPE,
              "neutral_hfa_applied": not args.no_neutral_hfa}

    # ---------------- STEP 1: V6 base ----------------
    # Production totals for the O/U column (constant across margin-only arms; noted, not tuned)
    for r in recs:
        r["model_total"] = r.get("model_total")

    usable = 0
    for r in recs:
        m = v6_margin(r, args.no_neutral_hfa)
        if m is None:
            continue
        r["model_margin"] = m
        usable += 1
    print(f"[v6] base margin computable on {usable} records")
    base = block("STEP1 v6-base", recs, args.seasons, args.holdout, report, "v6_base",
                 extra=f"hfa={'flat' if args.no_neutral_hfa else 'neutral-aware'}")

    if args.no_neutral_hfa:
        # sensitivity: does respecting neutral sites move anything?
        for r in recs:
            m = v6_margin(r, False)
            if m is not None:
                r["model_margin"] = m
        block("STEP1 v6-base (neutral-aware)", recs, args.seasons, args.holdout, report,
              "v6_base_neutral_aware", extra="sensitivity check")

    # ---------------- control: the V5 OLS regression ----------------
    import numpy as np
    train = [r for r in recs if r["season"] in fit_s]
    def feats(rs):
        out = []
        for r in rs:
            eh = r.get("exp_h"); ea = r.get("exp_a")
            eh = 50.0 if eh is None else eh
            ea = 50.0 if ea is None else ea
            out.append([r["sp_h"] - r["sp_a"], r["eff_h"] - r["eff_a"],
                        r["tal_h"] - r["tal_a"], eh - ea])
        return np.array(out, dtype=float)
    Xtr = np.hstack([np.ones((len(train), 1)), feats(train)])
    ytr = np.array([r["home_score"] - r["away_score"] for r in train], dtype=float)
    coef, *_ = np.linalg.lstsq(Xtr, ytr, rcond=None)
    print(f"[v6] control (V5 OLS on Level 1, fit {fit_s}): intercept {coef[0]:+.3f} "
          f"sp {coef[1]:+.4f} eff {coef[2]:+.4f} tal {coef[3]:+.4f} exp {coef[4]:+.4f}")
    Xall = np.hstack([np.ones((len(recs), 1)), feats(recs)])
    preds = Xall @ coef
    for r, p in zip(recs, preds):
        r["model_margin"] = float(p)
    block("STEP1 control v5-ols", recs, args.seasons, args.holdout, report, "v5_ols_control",
          extra="the regression V6 weights came from")
    report["v5_ols_control"]["coefficients"] = {
        "intercept": round(float(coef[0]), 4), "d_sp": round(float(coef[1]), 4),
        "d_eff": round(float(coef[2]), 4), "d_tal": round(float(coef[3]), 4),
        "d_exp": round(float(coef[4]), 4)}

    # ---------------- STEP 2: explosiveness add-on ----------------
    pbp = {}
    for s in args.seasons:
        p = ROOT / "data" / f"playbyplay_{s}.json"
        if p.exists():
            pbp[s] = json.loads(p.read_text(encoding="utf-8")).get("weeks", {})
    if not pbp:
        print("[v6] STEP2 skipped — no play-by-play cache (run scripts/harvest_plays.py)")
    else:
        rows = []
        for r in recs:
            def prior(team):
                acc = []
                for wk, teams in (pbp.get(r["season"]) or {}).items():
                    if int(wk) >= r["week"]:
                        continue
                    m = (teams or {}).get(team)
                    if m and isinstance(m.get("explosive_rate"), (int, float)):
                        acc.append(m["explosive_rate"])
                return sum(acc) / len(acc) if acc else None
            ph, pa = prior(r["home"]), prior(r["away"])
            if ph is None or pa is None:
                continue
            r["_d_expl"] = ph - pa
            if v6_margin(r, args.no_neutral_hfa) is not None:
                rows.append(r)
        print(f"[v6] STEP2 coverage: {len(rows)} games with prior-week explosiveness for BOTH teams")

        # baseline on the same subset for a like-for-like comparison
        for r in rows:
            r["model_margin"] = v6_margin(r, args.no_neutral_hfa)
        block("STEP2 v6-base (same subset)", rows, args.seasons, args.holdout, report,
              "step2_base_subset", extra="control on the add-on's coverage")
        contrib = [abs(r["_d_expl"]) for r in rows]
        contrib.sort()
        print(f"[v6] |d_explosive_rate| median {contrib[len(contrib)//2]:.4f}  "
              f"p90 {contrib[9*len(contrib)//10]:.4f}")

        grid_vals = [round(args.expl_step * i, 2)
                     for i in range(int(args.expl_max / args.expl_step) + 1)]
        best = (1e9, None)
        grid = []
        for c in grid_vals:
            for r in rows:
                r["model_margin"] = v6_margin(r, args.no_neutral_hfa, addon=c * r["_d_expl"])
            m = v3.ev(rows, fit_s)
            mae = m["abs_err"] / max(1, m["n"])
            grid.append({"weight_per_unit": c, "fit_mae": round(mae, 4)})
            if mae < best[0]:
                best = (mae, c)
        c = best[1]
        edge = c in (grid_vals[0], grid_vals[-1])
        print(f"[v6] STEP2 best add-on weight {c} pts per unit of explosive-rate diff "
              f"(fit MAE {best[0]:.4f}){'  [GRID-EDGE]' if edge else ''}")
        for r in rows:
            r["model_margin"] = v6_margin(r, args.no_neutral_hfa, addon=c * r["_d_expl"])
        res = block("STEP2 v6-base+explosiveness", rows, args.seasons, args.holdout, report,
                    "step2_explosiveness",
                    extra=f"weight {c}/unit -> typical add-on {c * contrib[len(contrib)//2]:+.2f} pts")
        res["weight_per_unit"] = c
        res["grid_edge"] = edge
        res["median_abs_contribution_pts"] = round(c * contrib[len(contrib)//2], 3)
        res["p90_abs_contribution_pts"] = round(c * contrib[9 * len(contrib) // 10], 3)
        res["grid"] = grid

    dest = Path(args.out) if args.out else OUT / f"v6_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    dest.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"[v6] report -> {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())