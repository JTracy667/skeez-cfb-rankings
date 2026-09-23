#!/usr/bin/env python3
"""backtest_v5.py — V5 work order: ARM 3 (quick combo), ARM 1 (regression), ARM 2 (data).

All on the same 5-year frozen inputs (2021-2025) as V2/V3/V4: fit 2021-24, held out 2025.
STANDING RULE: fit on MAE, never on ATS alone. Flag grid-edge optima.

ARM 3 — quick combo (runs first)
    V3's best ATS parameters (power 1.7) against V4's best calibration parameters
    (power 1.1, slope 0.3912), plus the two hybrids. Because V3 and V4 also differ in
    FORMULA FAMILY (V3 carries havoc/explosiveness/rest adjustments + HFA 2.5; V4's arm 1
    is just the nonlinear gap), every config is run under BOTH families. Otherwise a
    'combo' result would confound the parameter change with a formula change.

ARM 1 — regression model
    Replace the hand-tuned weighted sum with a model trained on actual margin (home-away).
    Inputs: V2 Level 1 (SP+, efficiency, talent, experience) + Level 2 (havoc, explosiveness,
    rest), all as home-minus-away differences. OLS first, then ridge, then gradient boosting.
    Coefficients are reported with 95% CIs and compared against V2's hand-set weights
    (sp 0.35 / eff 0.30 / tal 0.20 / exp 0.15) on a standardized-share basis, since the
    composite inputs do not share a variance scale.

Usage:
    python scripts/backtest_v5.py --seasons 2021 2022 2023 2024 2025 --holdout 2025 --arms 3
    python scripts/backtest_v5.py ... --arms 1
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

# Importing app starts the LIVE refresh scheduler (app.py:5163) -> real API spend.
os.environ.setdefault("REFRESH_INTERVAL_SECONDS", "0")

import backtest_v3 as v3  # noqa: E402

OUT = ROOT / "data" / "backtest_study"

V2_WEIGHTS = {"sp": 0.35, "eff": 0.30, "tal": 0.20, "exp": 0.15}

# ARM 3 configs: (label, power, slope) — slope None = refit by MAE at that power
ARM3_CONFIGS = [
    ("v3-best-ats",    1.7, None),      # V3's ATS winner, slope refit by MAE
    ("v4-best-cal",    1.1, 0.3912),    # V4's calibration winner
    ("hybrid-1.7-cal", 1.7, 0.3912),    # V3's power + V4's slope
    ("v3-refit-1.1",   1.1, None),      # V4's power, slope refit by MAE
]

FEATURES = ["d_sp", "d_eff", "d_tal", "d_exp", "d_havoc", "d_expl", "d_rest"]


def band_of(per: dict) -> float | None:
    vals = [v for v in per.values() if v is not None]
    return round(max(vals) - min(vals), 2) if vals else None


def block(label, recs, seasons, holdout, extra="") -> dict:
    """Pooled + per-season + held-out report, in the format the work order asks for."""
    pooled = v3.fmt(v3.ev(recs, None))
    hold = v3.fmt(v3.ev(recs, [holdout]))
    per = {str(s): v3.fmt(v3.ev(recs, [s]))["ats_fbs_pct"] for s in seasons}
    b = band_of(per)
    print(f"[v5] {label:<26} ATS {pooled['ats_fbs_pct']}% ({pooled['ats_fbs']}, {pooled['fbs_games']})  "
          f"O/U {pooled['ou_pct']}%  SU {pooled['su_pct']}%  MAE {pooled['mae_vs_book']}  "
          f"|m| {pooled['mean_model_margin']} vs {pooled['mean_book_line']}  bias {pooled['bias_vs_book']}  "
          f"band {b}pp  held-out {hold['ats_fbs_pct']}%{('  ' + extra) if extra else ''}")
    return {"pooled": pooled, "holdout": hold, "per_season": per, "band": b}


# ---------------------------------------------------------------------------------------
# ARM 3
# ---------------------------------------------------------------------------------------
def arm3(recs, seasons, holdout, report) -> None:
    fit_s = [s for s in seasons if s != holdout]
    for family in ("v3-with-adjustments", "gap-only"):
        print(f"\n[v5] === ARM 3 family: {family} ===")
        fam = {}
        for label, power, slope in ARM3_CONFIGS:
            p = dict(v3.BASE)
            p["power"] = power
            if slope is None:
                mae, slope = v3.fit_slope(recs, p, fit_s)
                edge = " [slope at grid edge]" if slope in (v3.SLOPE_GRID[0], v3.SLOPE_GRID[-1]) else ""
                print(f"[v5]   {label}: slope refit by MAE -> {slope} (fit MAE {mae:.3f}){edge}")
            p["slope"] = slope
            if family == "gap-only":
                p.update({"havoc": 0.0, "expl": 0.0, "rest": 0.0, "hfa": 0.0})
            v3.apply(recs, p)
            res = block(f"{label} [{family}]", recs, seasons, holdout)
            res["params"] = {k: p[k] for k in ("power", "slope", "havoc", "expl", "rest", "hfa")}
            fam[label] = res
        report["arm3"][family] = fam


# ---------------------------------------------------------------------------------------
# ARM 1 — regression
# ---------------------------------------------------------------------------------------
def _feat_row(r: dict):
    """Home-minus-away feature vector. None where an input is missing (imputed later)."""
    def num(v):
        return v if isinstance(v, (int, float)) else None

    d_sp = (r["sp_h"] - r["sp_a"]) if r.get("sp_h") is not None and r.get("sp_a") is not None else None
    d_eff = (r["eff_h"] - r["eff_a"]) if r.get("eff_h") is not None and r.get("eff_a") is not None else None
    d_tal = (r["tal_h"] - r["tal_a"]) if r.get("tal_h") is not None and r.get("tal_a") is not None else None
    eh, ea = num(r.get("exp_h")), num(r.get("exp_a"))
    d_exp = (eh - ea) if eh is not None and ea is not None else None
    hv = [r.get("hv_h"), r.get("hva_a"), r.get("hv_a"), r.get("hva_h")]
    d_havoc = ((hv[0] - hv[1]) - (hv[2] - hv[3])) if all(isinstance(x, (int, float)) for x in hv) else None
    ex = [r.get("expl_off_h"), r.get("expl_def_a"), r.get("expl_off_a"), r.get("expl_def_h")]
    d_expl = ((ex[0] - ex[1]) - (ex[2] - ex[3])) if all(isinstance(x, (int, float)) for x in ex) else None
    rh, ra = num(r.get("rest_home")), num(r.get("rest_away"))
    d_rest = (rh - ra) if rh is not None and ra is not None else None
    return [d_sp, d_eff, d_tal, d_exp, d_havoc, d_expl, d_rest]


def _matrix(rows, imp):
    import numpy as np
    Z = np.array([[v if v is not None else np.nan for v in _feat_row(r)] for r in rows], dtype=float)
    return np.where(np.isnan(Z), imp, Z)


def arm1(recs, seasons, holdout, report) -> None:
    import numpy as np
    from sklearn.ensemble import GradientBoostingRegressor
    from sklearn.linear_model import RidgeCV

    train = [r for r in recs if r["season"] != holdout]
    # impute with the TRAIN means, computed once and reused for every model
    imp = np.nanmean(np.array([[v if v is not None else np.nan for v in _feat_row(r)]
                               for r in train], dtype=float), axis=0)
    X = _matrix(train, imp)
    y = np.array([r["home_score"] - r["away_score"] for r in train], dtype=float)
    X_raw = np.array([[v if v is not None else np.nan for v in _feat_row(r)] for r in train], dtype=float)
    miss = {FEATURES[i]: int(np.isnan(X_raw[:, i]).sum()) for i in range(len(FEATURES))}
    print(f"[v5] arm1 train games {len(train)}  missing per feature: {miss}")

    Xd = np.hstack([np.ones((len(X), 1)), X])
    coef, *_ = np.linalg.lstsq(Xd, y, rcond=None)
    resid = y - Xd @ coef
    dof = max(1, len(y) - Xd.shape[1])
    sigma2 = float(resid @ resid) / dof
    cov = sigma2 * np.linalg.inv(Xd.T @ Xd)
    se = np.sqrt(np.diag(cov))
    r2 = 1 - (resid @ resid) / ((y - y.mean()) @ (y - y.mean()))

    names = ["intercept"] + FEATURES
    sd = X.std(axis=0)
    share_den = sum(abs(coef[1 + i]) * sd[i] for i in range(len(FEATURES))) or 1.0
    print("[v5] ARM 1 OLS coefficients (target = actual margin, home-minus-away):")
    coeffs = {}
    for i, nm in enumerate(names):
        share = (abs(coef[i]) * sd[i - 1] / share_den) if i > 0 else None
        ci = (coef[i] - 1.96 * se[i], coef[i] + 1.96 * se[i])
        sig = "significant" if (ci[0] > 0) == (ci[1] > 0) else "NOT significant"
        coeffs[nm] = {"coef": round(float(coef[i]), 4), "se": round(float(se[i]), 4),
                      "ci95": [round(float(ci[0]), 4), round(float(ci[1]), 4)],
                      "standardized_share": round(float(share), 3) if share is not None else None,
                      "verdict": sig}
        extra = f"  share {share:.3f}" if share is not None else ""
        print(f"[v5]   {nm:<9} coef {coef[i]:+8.4f}  95% CI [{ci[0]:+.4f}, {ci[1]:+.4f}]  {sig}{extra}")
    print(f"[v5] ARM 1 OLS R^2 (train) {r2:.4f}  resid sd {math.sqrt(sigma2):.2f}")

    ridge = RidgeCV(alphas=np.logspace(-2, 3, 30)).fit(X, y)
    gbm = GradientBoostingRegressor(random_state=0, n_estimators=300, max_depth=2,
                                    learning_rate=0.05).fit(X, y)

    def predict(model, rows):
        Z = np.array([[v if v is not None else np.nan for v in _feat_row(r)] for r in rows], dtype=float)
        Z = np.where(np.isnan(Z), imp, Z)
        return model(Z)

    def ols_pred(rows):
        Z = np.array([[v if v is not None else np.nan for v in _feat_row(r)] for r in rows], dtype=float)
        Z = np.where(np.isnan(Z), imp, Z)
        return np.hstack([np.ones((len(Z), 1)), Z]) @ coef

    report["arm1"] = {"features": FEATURES, "missing": miss,
                      "ols_coefficients": coeffs, "ols_r2_train": round(float(r2), 4),
                      "ols_resid_sd": round(math.sqrt(sigma2), 3),
                      "ridge_alpha": float(ridge.alpha_), "models": {}}

    def ols_batch(rows):
        Z = _matrix(rows, imp)
        return np.hstack([np.ones((len(Z), 1)), Z]) @ coef

    def ridge_batch(rows):
        return ridge.predict(_matrix(rows, imp))

    def gbm_batch(rows):
        return gbm.predict(_matrix(rows, imp))

    hold_rows = [r for r in recs if r["season"] == holdout]
    for mname, predictor in (("ols", ols_batch), ("ridge", ridge_batch), ("gbm", gbm_batch)):
        mae_vs_actual = {}
        for tag, rows in (("train", train), ("holdout", hold_rows)):
            p = predictor(rows)
            actual = np.array([r["home_score"] - r["away_score"] for r in rows], dtype=float)
            mae_vs_actual[tag] = round(float(np.mean(np.abs(p - actual))), 3)
        # one batched prediction pass, then attach
        for r, pred in zip(recs, predictor(recs)):
            r["model_margin"] = float(pred)
            if r.get("model_total") is None:
                r["model_total"] = 51.0
        res = block(f"arm1-{mname}-regression", recs, seasons, holdout,
                    extra=f"MAE vs ACTUAL margin: train {mae_vs_actual['train']} "
                          f"holdout {mae_vs_actual['holdout']}")
        res["mae_vs_actual_margin"] = mae_vs_actual
        report["arm1"]["models"][mname] = res

    report["arm1"]["v2_weights_for_comparison"] = V2_WEIGHTS


# ---------------------------------------------------------------------------------------
# ARM 2b — weekly play-by-play metrics as Level 2 inputs on top of V3 arm 1 (power 1.7)
# ---------------------------------------------------------------------------------------
PBP_FEATURES = ["d_havoc_rate", "d_off_ppa", "d_def_ppa", "d_epa_early", "d_explosive_rate",
                "d_sec_per_play"]


def load_pbp(seasons) -> dict:
    """season -> team -> week -> metrics, but ONLY weeks strictly before the game's week
    (using the game's own week would leak that game's plays into its own projection)."""
    out = {}
    for s in seasons:
        p = ROOT / "data" / f"playbyplay_{s}.json"
        if not p.exists():
            print(f"[v5] arm2b: missing {p.name} — run scripts/harvest_plays.py first")
            continue
        out[s] = json.loads(p.read_text(encoding="utf-8")).get("weeks", {})
    return out


def _pbp_prior(pbp: dict, season: int, team: str, week: int) -> dict:
    """Average every metric over weeks < week (empty dict when none)."""
    acc: dict[str, list] = {}
    for wk, teams in (pbp.get(season) or {}).items():
        if int(wk) >= week:
            continue
        m = (teams or {}).get(team)
        if not m:
            continue
        for k in ("havoc_rate", "off_ppa", "def_ppa", "epa_early", "explosive_rate", "sec_per_play"):
            v = m.get(k)
            if isinstance(v, (int, float)):
                acc.setdefault(k, []).append(v)
    return {k: sum(v) / len(v) for k, v in acc.items() if v}


def arm2b(recs, seasons, holdout, report) -> None:
    import numpy as np

    pbp = load_pbp(seasons)
    if not pbp:
        return
    fit_s = [s for s in seasons if s != holdout]

    rows = []
    for r in recs:
        h = _pbp_prior(pbp, r["season"], r["home"], r["week"])
        a = _pbp_prior(pbp, r["season"], r["away"], r["week"])
        if not h or not a:
            continue
        f = {}
        for k in ("havoc_rate", "off_ppa", "def_ppa", "epa_early", "explosive_rate", "sec_per_play"):
            f[f"d_{k}"] = (h[k] - a[k]) if (k in h and k in a) else None
        r["_pbp"] = f
        rows.append(r)
    print(f"[v5] arm2b coverage: {len(rows)} games with prior-week play-by-play for BOTH teams")

    # baseline on the SAME subset: V3 arm1 at power 1.7 with its MAE-fitted slope
    base = dict(v3.BASE)
    base["power"] = 1.7
    _, sl = v3.fit_slope(rows, base, fit_s)
    base["slope"] = sl
    v3.apply(rows, base)
    for r in rows:
        r["_base_margin"] = r["model_margin"]
    block("arm2b-baseline(arm1@1.7, same subset)", rows, seasons, holdout,
          extra=f"slope {sl}")

    imp = {}
    Xtr = []
    for r in rows:
        if r["season"] not in fit_s:
            continue
        Xtr.append([r["_pbp"].get(k) for k in PBP_FEATURES])
    A = np.array([[np.nan if v is None else v for v in row] for row in Xtr], dtype=float)
    imp = np.nanmean(A, axis=0)
    X = np.where(np.isnan(A), imp, A)
    resid = np.array([r["home_score"] - r["away_score"] - r["_base_margin"]
                      for r in rows if r["season"] in fit_s], dtype=float)
    Xd = np.hstack([np.ones((len(X), 1)), X])
    coef, *_ = np.linalg.lstsq(Xd, resid, rcond=None)
    print("[v5] arm2b residual-regression coefficients (target = actual margin - arm1 margin):")
    for i, nm in enumerate(["intercept"] + PBP_FEATURES):
        print(f"[v5]   {nm:<16} {coef[i]:+9.4f}")

    def with_addon(rows_x, addon_fn):
        out = []
        for r in rows_x:
            v = [r["_pbp"].get(k) for k in PBP_FEATURES]
            v = [imp[j] if x is None else x for j, x in enumerate(v)]
            r["model_margin"] = r["_base_margin"] + addon_fn(v)
            out.append(r)
        return out

    # (1) the fitted residual model
    res_ls = block("arm2b-+fitted-residual-model", with_addon(rows, lambda v: float(np.dot(coef[1:], v))),
                   seasons, holdout, extra="OLS on the arm1 residual")
    res_ls["coefficients"] = {nm: round(float(coef[i]), 4)
                              for i, nm in enumerate(["intercept"] + PBP_FEATURES)}
    report.setdefault("arm2b", {})["ls"] = res_ls
    report["arm2b"]["coverage"] = len(rows)
    report["arm2b"]["pbp_features"] = PBP_FEATURES

    # (2) one standardized composite, scale fitted on MAE (the standing rule)
    Ztr = np.where(np.isnan(A), imp, A)
    sd = Ztr.std(axis=0)
    sd[sd == 0] = 1.0
    mean = imp

    def composite(r):
        v = [(r["_pbp"].get(k)) for k in PBP_FEATURES]
        v = [mean[j] if x is None else x for j, x in enumerate(v)]
        return float(sum((v[j] - mean[j]) / sd[j] for j in range(len(v))))

    best = (1e9, 0.0)
    for c in [round(-2.0 + 0.1 * i, 2) for i in range(41)]:
        for r in rows:
            r["model_margin"] = r["_base_margin"] + c * composite(r)
        m = v3.ev(rows, fit_s)
        mae = m["abs_err"] / max(1, m["n"])
        if mae < best[0]:
            best = (mae, c)
    c = best[1]
    for r in rows:
        r["model_margin"] = r["_base_margin"] + c * composite(r)
    res_z = block("arm2b-+composite(MAE-fit scale)", rows, seasons, holdout,
                  extra=f"scale {c} (fit MAE {best[0]:.3f})")
    res_z["scale"] = c
    res_z["scale_at_grid_edge"] = c in (-2.0, 2.0)
    report["arm2b"]["composite"] = res_z


# ---------------------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="V5 arms")
    ap.add_argument("--seasons", type=int, nargs="+", default=[2021, 2022, 2023, 2024, 2025])
    ap.add_argument("--holdout", type=int, default=2025)
    ap.add_argument("--arms", default="3,1")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    arms = {a.strip() for a in args.arms.split(",")}
    print(f"[v5] loading records for {args.seasons}")
    recs = v3.load(args.seasons)
    print(f"[v5] {len(recs)} records")
    report = {"seasons": args.seasons, "holdout": args.holdout, "arm3": {}, "arm1": {}}

    if "3" in arms:
        arm3(recs, args.seasons, args.holdout, report)
    if "1" in arms:
        arm1(recs, args.seasons, args.holdout, report)
    if "2b" in arms or "2" in arms:
        arm2b(recs, args.seasons, args.holdout, report)

    dest = Path(args.out) if args.out else OUT / f"v5_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    dest.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"[v5] report -> {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())