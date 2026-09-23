#!/usr/bin/env python3
"""backtest_v3.py — structural changes + pick-selection filter (V3 work order, arms 1-4).

ARM 1  nonlinear margin curve: margin = sign(diff) * |diff|^power * slope + adj + HFA
       power 0.8..2.0 step 0.1, slope refit at each power.
ARM 2  pick-selection threshold: keep only picks with |edge| >= threshold (0..7 step 1).
       Applied to v2-baseline and v2-finetune-market. Reported against TWO edge definitions,
       because "|model_edge|" is ambiguous and they answer different questions:
         (a) |composite edge|  = how lopsided we think the teams are
         (b) |model - close|   = how much we disagree with the market
ARM 3  best power + best threshold combined.
ARM 4  line movement: line_move_adj = (open_spread - close_spread) * move_factor, 0.0..1.0.
       Sign check: CFBD spread is home-perspective with favourites NEGATIVE, so
       (open - close) > 0 means the home team got MORE favoured (informed money on home),
       and a margin in home points must therefore rise. Added with a positive sign.

ARM 5 (separate totals model) is NOT here — it needs a design decision on which inputs and a
/games/weather fetch.

Fit on 2021-2024, held out 2025. Grid-edge optima are marked.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from backtest_study import build_records, evaluate, fmt, objective  # noqa: E402
from backtest_v2 import PROBE, V2_W, neutral_sites, pit_explosiveness, probe_norm  # noqa: E402
from backtest_v2_finetune import clamp  # noqa: E402

CACHE = ROOT / "data" / "backtest_cache"
OUT = ROOT / "data" / "backtest_study"

POWER_GRID = [round(0.8 + 0.1 * i, 2) for i in range(13)]          # 0.8 .. 2.0
SLOPE_GRID = [round(0.01 * (1.13 ** i), 4) for i in range(48)]     # 0.01 .. ~3.0 log-spaced
THRESH_GRID = [0, 1, 2, 3, 4, 5, 6, 7]
MOVE_GRID = [round(0.1 * i, 2) for i in range(11)]                 # 0.0 .. 1.0

BASE = {"slope": 0.6, "hfa": 2.5, "havoc": 3.0, "expl": 3.0, "rest": 2.0, "power": 1.0, "move": 0.0}
FITTED_MARKET_K = 0.05


def load(seasons):
    recs = []
    for s in seasons:
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
            # arm 4 input: negative of the usual movement, in the home-margin frame
            so, sc = r.get("spread_open"), r.get("spread_close")
            r["move"] = (so - sc) if (so is not None and sc is not None) else 0.0
        recs.extend(rs)
    for key in PROBE:
        probe_norm(recs, key)
    for r in recs:
        r["edge"] = (V2_W["sp"] * r["sp_h"] + V2_W["eff"] * r["eff_h"] + V2_W["tal"] * r["tal_h"]
                     + V2_W["exp"] * (r["exp_h"] if r["exp_h"] is not None else 50.0)
                     - (V2_W["sp"] * r["sp_a"] + V2_W["eff"] * r["eff_a"] + V2_W["tal"] * r["tal_a"]
                        + V2_W["exp"] * (r["exp_a"] if r["exp_a"] is not None else 50.0)))
        avg = ((r["sp_h"] * V2_W["sp"] + r["eff_h"] * V2_W["eff"] + r["tal_h"] * V2_W["tal"]
                + V2_W["exp"] * (r["exp_h"] if r["exp_h"] is not None else 50.0))
               + (r["sp_a"] * V2_W["sp"] + r["eff_a"] * V2_W["eff"] + r["tal_a"] * V2_W["tal"]
                  + V2_W["exp"] * (r["exp_a"] if r["exp_a"] is not None else 50.0))) / 2.0
        r["model_total"] = round(51.0 + (avg - 50.0) * 0.10, 1)
    return recs


def adj(r, p):
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
    a += r.get("move", 0.0) * p.get("move", 0.0)
    return a


def apply(recs, p, mode="model", k=FITTED_MARKET_K):
    for r in recs:
        d = r["edge"]
        hfa = 0.0 if r["neutral"] else p["hfa"]
        if mode == "market":
            r["model_margin"] = d * k
        else:
            nl = math.copysign(abs(d) ** p["power"], d) * p["slope"]
            r["model_margin"] = nl + adj(r, p) + hfa


def ev(recs, seasons=None, mode="model", k=FITTED_MARKET_K):
    arm = {"margin": "market", "prior": "open"} if mode == "market" else {"margin": "model"}
    return evaluate(recs, arm, k, 0.0, seasons)


def fit_slope(recs, p, fit_seasons, mode="model"):
    """Fit slope by MINIMISING MAE, not by maximising ATS.

    Maximising ATS alone is a gameable objective: as slope -> 0 the margin collapses toward a
    constant pick ("always the home underdog"), which scores ~51% by construction while SU falls
    and MAE explodes. Measured on 2021-24 the ATS-maximising slope came out at 0.014 with SU 61.1%
    and MAE 11.66 — a degenerate model, not a better one. Calibration cannot be gamed by shrining.
    Returns (mae, slope).
    """
    best = (1e9, p["slope"])
    for s in SLOPE_GRID:
        trial = dict(p); trial["slope"] = s
        apply(recs, trial, mode)
        m = ev(recs, fit_seasons, mode)
        mae = m["abs_err"] / max(1, m["n"])
        if mae < best[0]:
            best = (mae, s)
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", type=int, nargs="+", required=True)
    ap.add_argument("--holdout", type=int, default=2025)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    recs = load(args.seasons)
    print(f"[v3] {len(recs)} records")
    fit_s = [s for s in args.seasons if s != args.holdout]
    report = {"seasons": args.seasons, "holdout": args.holdout, "arms": {}}

    def block(name, p, mode="model", k=FITTED_MARKET_K, extra=None):
        apply(recs, p, mode, k)
        pooled = fmt(ev(recs, None, mode, k))
        hold = fmt(ev(recs, [args.holdout], mode, k))
        per = {str(s): fmt(ev(recs, [s], mode, k))["ats_fbs_pct"] for s in args.seasons}
        band = (max(v for v in per.values() if v is not None)
                - min(v for v in per.values() if v is not None))
        report["arms"][name] = {"pooled": pooled, "holdout": hold, "per_season": per,
                                "band": round(band, 2),
                                "params": {**p, **({"k": k} if mode == "market" else {})},
                                "extra": extra or {}}
        print(f"[v3] {name:<26} ATS {pooled['ats_fbs_pct']}% ({pooled['ats_fbs']})  "
              f"O/U {pooled['ou_pct']}%  SU {pooled['su_pct']}%  MAE {pooled['mae_vs_book']}  "
              f"|m| {pooled['mean_model_margin']} vs {pooled['mean_book_line']}  band {round(band,2)}pp  "
              f"held-out {hold['ats_fbs_pct']}%")
        return report["arms"][name]

    # ---------- ARM 0: v2-baseline reference at the accepted values ----------
    base = dict(BASE)
    _, base["slope"] = fit_slope(recs, base, fit_s)
    block("v2-baseline(ref)", base)

    # ---------- ARM 1: nonlinear margin curve ----------
    best = (-1.0, None, None)
    rows = []
    for pw in POWER_GRID:
        trial = dict(BASE); trial["power"] = pw
        _, sl = fit_slope(recs, trial, fit_s)
        trial["slope"] = sl
        apply(recs, trial)
        m = ev(recs, fit_s)
        v = objective(m)
        rows.append({"power": pw, "slope": sl, "ats_pct": round(100 * v, 1),
                     "mean_margin": round(sum(abs(r["model_margin"]) for r in recs
                                              if r["season"] in fit_s) / max(1, m["n"]), 2)})
        if v > best[0]:
            best = (v, pw, sl)
    print("[v3] arm1 sweep (fit seasons):")
    for r in rows:
        print(f"        power {r['power']:<5} slope {r['slope']:<8} ATS {r['ats_pct']}%  "
              f"mean|margin| {r['mean_margin']}")
    p1 = dict(BASE); p1["power"] = best[1]; p1["slope"] = best[2]
    a1 = block("arm1-nonlinear", p1, extra={
        "power": best[1], "slope": best[2],
        "power_at_edge": best[1] in (POWER_GRID[0], POWER_GRID[-1]),
        "sweep_on_fit_seasons": rows})

    # gap-closing variant: slope chosen so mean|margin| matches the book, not ATS-optimal
    fitr = [r for r in recs if r["season"] in fit_s]
    book_mean = sum(abs(r["spread_close"]) for r in fitr) / len(fitr)
    p1g = dict(p1); p1g["slope"] = 1.0
    for _ in range(40):
        base_nl = sum(abs(math.copysign(abs(r["edge"]) ** p1["power"], r["edge"]))
                      for r in fitr) / len(fitr)
        p1g["slope"] = book_mean / base_nl
    a1g = block("arm1-gapclosing", p1g, extra={"target_book_mean": round(book_mean, 2),
                                               "note": "slope set to match book, NOT ATS-optimal"})

    # ---------- ARM 2: pick-selection thresholds ----------
    def thresholds(label, mode, p, k=FITTED_MARKET_K):
        apply(recs, p, mode, k)
        if mode == "market":
            arm = {"margin": "market", "prior": "open"}

            def em(r):
                so = r.get("spread_open")
                return None if so is None else (-so) + k * r["edge"]
        else:
            arm = {"margin": "model"}

            def em(r):
                return r["model_margin"]

        out = {}
        for th in THRESH_GRID:
            for kind in ("edge", "disagree"):
                if kind == "edge":
                    sub = [r for r in recs if abs(r["edge"]) >= th]
                else:
                    sub = [r for r in recs
                           if em(r) is not None and abs(em(r) - (-r["spread_close"])) >= th]
                pooled = fmt(evaluate(sub, arm, k, 0.0))
                held = fmt(evaluate(sub, arm, k, 0.0, seasons=[args.holdout]))
                out.setdefault(str(th), {})[kind] = {
                    "picks": pooled["fbs_games"], "ats": pooled["ats_fbs"],
                    "ats_pct": pooled["ats_fbs_pct"], "ou_pct": pooled["ou_pct"],
                    "holdout_ats_pct": held["ats_fbs_pct"]}
        report["arms"][f"arm2-{label}"] = {"thresholds": out, "params": {**p, **({"k": k} if mode == "market" else {})}}
        print(f"[v3] arm2-{label}: threshold sweep (ATS% by |edge| / |model-book|)")
        for th in THRESH_GRID:
            e = out[str(th)]["edge"]; d = out[str(th)]["disagree"]
            print(f"        >={th}: edge {e['ats_pct']}% ({e['picks']} picks, held-out "
                  f"{e['holdout_ats_pct']}%)   disagree {d['ats_pct']}% ({d['picks']} picks, "
                  f"held-out {d['holdout_ats_pct']}%)")
        return out

    t_base = thresholds("v2-baseline", "model", base)
    t_mkt = thresholds("v2-finetune-market", "market", base, FITTED_MARKET_K)

    # ---------- ARM 3: best power + best threshold ----------
    def best_thresh(out, kind="disagree"):
        cand = [(out[str(th)][kind]["ats_pct"], th) for th in THRESH_GRID
                if (out[str(th)][kind]["picks"] or 0) >= 300 and out[str(th)][kind]["ats_pct"]]
        return max(cand)[1] if cand else 0

    th3 = best_thresh(t_base)
    apply(recs, p1)
    sub = [r for r in recs if abs(r["model_margin"] - (-r["spread_close"])) >= th3]
    pooled = fmt(evaluate(sub, {"margin": "model"}))
    held = fmt(evaluate(sub, {"margin": "model"}, seasons=[args.holdout]))
    per = {str(s): fmt(evaluate([r for r in sub if r["season"] == s], {"margin": "model"}))["ats_fbs_pct"]
           for s in args.seasons}
    report["arms"]["arm3-combined"] = {"threshold": th3, "pooled": pooled, "holdout": held,
                                       "per_season": per, "params": p1,
                                       "extra": {"threshold_kind": "|model - close|"}}
    print(f"[v3] arm3-combined (power {p1['power']}, threshold {th3}): "
          f"ATS {pooled['ats_fbs_pct']}% ({pooled['ats_fbs']}, {pooled['fbs_games']} picks)  "
          f"held-out {held['ats_fbs_pct']}%")

    # ---------- ARM 4: line movement ----------
    bm = (1e9, 0.0)
    for mf in MOVE_GRID:
        trial = dict(base); trial["move"] = mf
        apply(recs, trial)
        m = ev(recs, fit_s)
        mae = m["abs_err"] / max(1, m["n"])
        if mae < bm[0]:          # MAE again, not ATS — see fit_slope
            bm = (mae, mf)
    p4 = dict(base); p4["move"] = bm[1]
    block("arm4-linemove", p4, extra={"move_factor": bm[1],
                                      "move_at_edge": bm[1] in (MOVE_GRID[0], MOVE_GRID[-1])})

    dest = Path(args.out) if args.out else OUT / f"v3_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    dest.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"[v3] report -> {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())