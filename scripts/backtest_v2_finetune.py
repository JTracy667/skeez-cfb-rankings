#!/usr/bin/env python3
"""backtest_v2_finetune.py — knob-turning within V2 as built. No structural changes.

Parameters (all fitted):
  slope       0.50 .. 0.80 step 0.025
  HFA         1.50 .. 4.00 step 0.25   (fitted on NON-NEUTRAL games only)
  havoc       1.0 .. 5.0  step 0.5     outer scale (+-N pts about the clamp(+-1.0) net)
  expl        1.0 .. 5.0  step 0.5     outer scale (+-N pts about the clamp(+-0.5) net)
  rest        0.0 .. 4.0  step 0.5     cap on (rest_diff x 0.3)

Method: coordinate descent (3 passes) on 2021-2024; held out 2025. Then leave-one-season-out x5
(refit on the other four, score the fifth) so the headline is not an in-sample artifact.
Interior vs grid-edge is reported for every parameter — a parameter sitting on an edge means the
optimum is outside the grid or the surface is flat.

Arms:
  v2-finetune         all five params jointly optimised, model frame
  v2-finetune-market  market-anchor frame: opening_line + k * composite_edge (k fitted)
  v2-noclamp          as v2-finetune but Level-1 normalisations UNCLAMPED (tails freed), to test
                      whether the margin-compression gap (model |margin| ~7.3 vs book ~12.4) is
                      caused by the [0,100] clamps.

The unclamped normalisations are reimplemented from app.py (they cannot be obtained from the
production probe, which returns post-clamp values). The reimplementation is VALIDATED against the
production clamped output before use — see validate_norms().
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from backtest_study import build_records, evaluate, fmt, objective  # noqa: E402
from backtest_v2 import (HFA, PROBE, V2_W, neutral_sites, pit_explosiveness,  # noqa: E402
                         probe_norm)

CACHE = ROOT / "data" / "backtest_cache"
OUT = ROOT / "data" / "backtest_study"

SLOPE_GRID = [round(0.50 + 0.025 * i, 3) for i in range(13)]      # 0.50 .. 0.80
HFA_GRID = [round(1.50 + 0.25 * i, 2) for i in range(11)]         # 1.50 .. 4.00
HAVOC_GRID = [round(1.0 + 0.5 * i, 2) for i in range(9)]          # 1.0 .. 5.0
EXPL_GRID = [round(1.0 + 0.5 * i, 2) for i in range(9)]           # 1.0 .. 5.0
REST_GRID = [round(0.5 * i, 2) for i in range(9)]                 # 0.0 .. 4.0
START = {"slope": 0.6, "hfa": 2.5, "havoc": 3.0, "expl": 3.0, "rest": 2.0}


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


# --- Level-1 normalisations, reimplemented from app.py:2798-2864 --------------------------
def _norms(td: dict, cl: bool):
    """Return (sp, eff, tal) normalised 0-100 (cl) or unclamped. Formulas copied from app.py."""
    def g(lo, hi, x):
        return clamp(x, lo, hi) if cl else x

    sp_plus = td.get("sp_plus")
    sp = g(0, 100, ((sp_plus + 40) / 80 * 100) if sp_plus is not None else 50.0)

    # app.py:2790 — the missing-recruiting default is 80, NOT 130. A wrong default here silently
    # shifts talent_norm for every team without recruiting data.
    rec_rank = td.get("recruiting_rank") or 80
    rec_norm = g(0, 100, (130 - rec_rank) / 129 * 100)
    ts = td.get("talent_score")
    if ts:
        tc = g(0, 100, (ts - 250) / 750 * 100)
        program = tc * 0.70 + rec_norm * 0.30
    else:
        program = rec_norm
    pct_ret = td.get("pct_ppa_returning")
    ret_norm = g(0, 100, (pct_ret if pct_ret is not None else 50.0))
    tal = program * 0.6 + ret_norm * 0.4

    off_sr, def_sr = td.get("off_success_rate"), td.get("def_success_rate")
    sr = g(0, 100, (off_sr - def_sr + 0.20) / 0.40 * 100) if None not in (off_sr, def_sr) else 50.0
    epa = td.get("epa_play") or 0.0
    def_epa = td.get("def_epa_play") or 0.0
    epa_n = g(0, 100, (epa - def_epa + 0.40) / 0.80 * 100)
    off_ppo, def_ppo = td.get("off_ppo"), td.get("def_ppo")
    ppo = g(0, 100, (off_ppo - def_ppo + 2.5) / 5.0 * 100) if None not in (off_ppo, def_ppo) else 50.0
    vals = (td.get("off_line_yards"), td.get("def_line_yards"),
            td.get("off_stuff_rate"), td.get("def_stuff_rate"))
    if all(v is not None for v in vals):
        ly = g(0, 100, (vals[0] - vals[1] + 1.5) / 3.0 * 100)
        st = g(0, 100, (vals[3] - vals[2] + 0.15) / 0.30 * 100)
        trench = ly * 0.6 + st * 0.4
    else:
        trench = 50.0
    pp, dpp = td.get("pts_per_poss"), td.get("def_pts_per_poss")
    ppd = g(0, 100, (pp - dpp + 2.0) / 4.0 * 100) if None not in (pp, dpp) else 50.0
    eff = sr * 0.28 + epa_n * 0.24 + ppo * 0.16 + trench * 0.16 + ppd * 0.16
    return sp, eff, tal


def validate_norms(recs: list[dict]) -> None:
    """My clamped reimplementation must reproduce production's probe values, or the unclamped
    variant is built on a formula I misread."""
    worst, checked = 0.0, 0
    for r in recs:
        for side, pit in (("h", r["home_pit"]), ("a", r["away_pit"])):
            sp, eff, tal = _norms(pit, True)
            for key, mine in (("sp", sp), ("eff", eff), ("tal", tal)):
                got = r.get(f"{key}_{side}")
                if got is None:
                    continue
                worst = max(worst, abs(mine - got))
                checked += 1
    print(f"[ft] normalisation validation: {checked} comparisons, max |mine - production| "
          f"= {worst:.6f}")
    if worst > 0.20:
        raise SystemExit("reimplemented norms do NOT match production — aborting")


def build(recs: list[dict]) -> None:
    for key in PROBE:
        probe_norm(recs, key)
    validate_norms(recs)
    for r in recs:
        for side, pit in (("h", r["home_pit"]), ("a", r["away_pit"])):
            sp, eff, tal = _norms(pit, False)
            r[f"sp_{side}_u"], r[f"eff_{side}_u"], r[f"tal_{side}_u"] = sp, eff, tal


def comp(r: dict, side: str, w: dict, unclamped: bool) -> float:
    sfx = "_u" if unclamped else ""
    exp = r.get(f"exp_{side}")
    exp = 50.0 if exp is None else exp
    return (w["sp"] * r[f"sp_{side}{sfx}"] + w["eff"] * r[f"eff_{side}{sfx}"]
            + w["tal"] * r[f"tal_{side}{sfx}"] + w["exp"] * exp)


def adj(r: dict, p: dict) -> float:
    a = 0.0
    if None not in (r.get("hv_h"), r.get("hva_a"), r.get("hv_a"), r.get("hva_h")):
        net = (r["hv_h"] - r["hva_a"]) - (r["hv_a"] - r["hva_h"])
        a += clamp(net, -1.0, 1.0) * p["havoc"]
    if None not in (r.get("expl_off_h"), r.get("expl_def_a"),
                    r.get("expl_off_a"), r.get("expl_def_h")):
        net = (r["expl_off_h"] - r["expl_def_a"]) - (r["expl_off_a"] - r["expl_def_h"])
        a += clamp(net, -0.5, 0.5) * p["expl"]
    rh, ra = r.get("rest_home"), r.get("rest_away")
    if rh is not None and ra is not None:
        a += clamp((rh - ra) * 0.3, -p["rest"], p["rest"])
    return a


def apply(recs: list[dict], w: dict, p: dict, unclamped=False, mode="model", k=0.0) -> None:
    for r in recs:
        avg = (comp(r, "h", w, unclamped) + comp(r, "a", w, unclamped)) / 2.0
        r["model_total"] = round(51.0 + (avg - 50.0) * 0.10, 1)
        r["edge"] = comp(r, "h", w, unclamped) - comp(r, "a", w, unclamped)
        hfa = 0.0 if r["neutral"] else p["hfa"]
        if mode == "market":
            r["model_margin"] = r["edge"] * k
        else:
            r["model_margin"] = r["edge"] * p["slope"] + adj(r, p) + hfa


def sc(recs, w, p, unclamped, mode="model", k=0.0, seasons=None, nonneutral_only=False):
    apply(recs, w, p, unclamped, mode, k)
    arm = {"margin": "market", "prior": "open"} if mode == "market" else {"margin": "model"}
    ev = evaluate(recs, arm, k, 0.0, seasons)
    if nonneutral_only:
        pass
    return ev


def fit(recs, w, unclamped, seasons, mode="model", passes=3, log=None):
    p = {"slope": START["slope"], "hfa": START["hfa"], "havoc": START["havoc"],
         "expl": START["expl"], "rest": START["rest"]}
    k = 0.05
    grids = [("slope", SLOPE_GRID), ("hfa", HFA_GRID), ("havoc", HAVOC_GRID),
             ("expl", EXPL_GRID), ("rest", REST_GRID)]
    if mode == "market":
        grids = [("k", [round(0.05 * i, 2) for i in range(21)])] + grids[2:]
    for _ in range(passes):
        for name, grid in grids:
            best_v = objective(sc(recs, w, p, unclamped, mode, k, seasons))
            best_g = (k if name == "k" else p[name])
            for g in grid:
                if name == "k":
                    v = objective(sc(recs, w, p, unclamped, mode, g, seasons))
                else:
                    trial = dict(p); trial[name] = g
                    v = objective(sc(recs, w, p if False else trial, unclamped, mode, k, seasons))
                if v > best_v:
                    best_v, best_g = v, g
            if name == "k":
                k = best_g
            else:
                p[name] = best_g
    return p, k


def edges(p, k, mode):
    out = {}
    if mode == "market":
        out["k"] = "edge" if k in (0.0, 1.0) else "interior"
    else:
        out["slope"] = "edge" if p["slope"] in (SLOPE_GRID[0], SLOPE_GRID[-1]) else "interior"
        out["hfa"] = "edge" if p["hfa"] in (HFA_GRID[0], HFA_GRID[-1]) else "interior"
        for key, grid in (("havoc", HAVOC_GRID), ("expl", EXPL_GRID), ("rest", REST_GRID)):
            out[key] = "edge" if p[key] in (grid[0], grid[-1]) else "interior"
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", type=int, nargs="+", required=True)
    ap.add_argument("--holdout", type=int, default=2025)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    recs: list[dict] = []
    for s in args.seasons:
        rs = build_records(s, None)
        ns = neutral_sites(s)
        expl = pit_explosiveness(s)
        hv = json.loads((CACHE / f"havoc_{s-1}.json").read_text(encoding="utf-8")) \
            if (CACHE / f"havoc_{s-1}.json").exists() else {}
        exp = json.loads((CACHE / f"experience_{s}.json").read_text(encoding="utf-8")) \
            if (CACHE / f"experience_{s}.json").exists() else {}
        for r in rs:
            r["neutral"] = ns.get(str(r["game_id"]), False)
            h, a, wk = r["home"], r["away"], r["week"]
            r["exp_h"] = (exp.get(h) or {}).get("experience_score")
            r["exp_a"] = (exp.get(a) or {}).get("experience_score")
            hh, aa = hv.get(h) or {}, hv.get(a) or {}
            r["hv_h"], r["hva_h"] = hh.get("def_havoc"), hh.get("havoc_allowed")
            r["hv_a"], r["hva_a"] = aa.get("def_havoc"), aa.get("havoc_allowed")
            po, pd = expl.get((h, wk)), expl.get((a, wk))
            r["expl_off_h"], r["expl_def_h"] = po if po else (None, None)
            r["expl_off_a"], r["expl_def_a"] = pd if pd else (None, None)
        recs.extend(rs)
    print(f"[ft] {len(recs)} records over {args.seasons}")
    build(recs)

    fit_seasons = [s for s in args.seasons if s != args.holdout]
    report = {"seasons": args.seasons, "holdout": args.holdout, "arms": {}}

    def summarize(name, w, unclamped, mode="model"):
        p, k = fit(recs, w, unclamped, fit_seasons, mode)
        pooled = fmt(sc(recs, w, p, unclamped, mode, k))
        hold = fmt(sc(recs, w, p, unclamped, mode, k, [args.holdout]))
        per = {str(s): fmt(sc(recs, w, p, unclamped, mode, k, [s]))["ats_fbs_pct"]
               for s in args.seasons}
        # leave-one-season-out x5
        loso = {}
        for hs in args.seasons:
            pp, kk = fit(recs, w, unclamped, [s for s in args.seasons if s != hs], mode)
            loso[str(hs)] = fmt(sc(recs, w, pp, unclamped, mode, kk, [hs]))["ats_fbs_pct"]
        loos = [v for v in loso.values() if v is not None]
        report["arms"][name] = {
            "pooled": pooled, "holdout_2025": hold, "per_season": per, "loso": loso,
            "params": {**p, "k": k} if mode == "market" else p,
            "grid_position": edges(p, k, mode),
            "in_sample_ats_pct": None,
            "band": (max(per.values()) - min(per.values())) if per else None,
        }
        band = report["arms"][name]["band"]
        print(f"[ft] {name:<20} ATS-FBS {pooled['ats_fbs_pct']}% ({pooled['ats_fbs']})  "
              f"O/U {pooled['ou_pct']}%  SU {pooled['su_pct']}%  MAE {pooled['mae_vs_book']}  "
              f"|margin| {pooled['mean_model_margin']} vs book {pooled['mean_book_line']}  "
              f"band {band}pp")
        print(f"[ft] {'':<20} params {report['arms'][name]['params']}")
        print(f"[ft] {'':<20} grid {report['arms'][name]['grid_position']}  "
              f"held-out {args.holdout}: {hold['ats_fbs_pct']}%  LOSO {loso}")
        return report["arms"][name]

    summarize("v2-finetune", V2_W, False, "model")
    summarize("v2-finetune-market", V2_W, False, "market")
    summarize("v2-noclamp", V2_W, True, "model")

    dest = Path(args.out) if args.out else OUT / f"v2ft_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    dest.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"[ft] report -> {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())