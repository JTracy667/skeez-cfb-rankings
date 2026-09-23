"""backtest_talent_arms.py — is the RECRUITING half of the talent term earning its weight?

MOTIVATION (Jeff, Sep 2026): the live board ranks Oregon #3 with an AP rank of 20, and the
suspicion is that recruiting is rewarded too highly — a signing class has not taken a snap yet,
and freshmen rarely start on elite teams. The talent term is the largest in the composite (0.32)
and is built as:

    program_talent = 247 Team Talent Composite (85-man ROSTER) x 0.70 + recruiting class rank x 0.30
    talent_norm    = program_talent x 0.60 + returning production x 0.40

The 247 composite is a ROSTER measure, so it needs no lag. `recruiting_rank` is the INCOMING
CLASS, so crediting it in the same season is a one-year-ahead error. This study isolates that.

ARMS
  A base        : V6.1 blend, unchanged (control)
  B lag         : recruiting_rank from the PRIOR signing class (s-1) — Jeff's hypothesis
  C roster-only : drop the class rank from program_talent (247 composite alone)
  D fit         : fit the (class-rank share, returning share) grid by MAE instead of hardcoding
                  0.70/0.30 and 0.60/0.40

DESIGN: change ONE thing per arm — the talent blend — and hold slope/HFA at the V6.1 values
(0.65 / 3.50) so the comparison is about the weighting, not the scale. Arms are expressed by
REWRITING the input fields so the app's own formula yields the target blend: the app computes
0.6*(0.7*tc + 0.3*rec) + 0.4*ret, so setting tc' = rec' = u makes it compute u exactly, and
setting all three to u makes it compute u. That is exact for every blend, no clipping.

FIT: 2022-2024 (the lagged arm needs s-1, so 2021 is out for every arm to keep the SAME BYTES).
HOLDOUT: 2025. Base is also reported on 2021-2024 as a check that it reproduces the published
V6.1 numbers.

  usage: python scripts/backtest_talent_arms.py [--seasons 2022 2023 2024] [--holdout 2025]
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
CACHE = ROOT / "data" / "backtest_cache"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("REFRESH_INTERVAL_SECONDS", "0")  # never start the live scheduler

POWER = 1.1          # V6.1 margin family
SLOPE = 0.65         # V6.1 fitted slope
HFA = 3.50           # V6.1 fitted home advantage
W = {"sp_plus": 0.185567, "efficiency": 0.309278, "talent": 0.319588, "experience": 0.185567}


def clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


# ---------------------------------------------------------------- talent components
def tc_norm(pit: dict):
    """247 85-man roster talent composite -> 0-100 (None when absent)."""
    ts = pit.get("talent_score")
    return None if ts is None else clamp((ts - 250) / 750 * 100)


def rec_norm(pit: dict):
    rk = pit.get("recruiting_rank")
    return None if not rk else clamp((130 - rk) / 129 * 100)


def ret_norm(pit: dict):
    p = pit.get("pct_ppa_returning")
    return clamp(50.0 if p is None else p)


def talent_blend(pit: dict, a: float = 0.70, r: float = 0.60) -> float:
    """a = share of program_talent from the 247 ROSTER composite; r = share of talent_norm
    from program_talent (1-r goes to returning production)."""
    tc, rc = tc_norm(pit), rec_norm(pit)
    if tc is None and rc is None:
        program = 50.0
    elif tc is None:
        program = rc
    elif rc is None:
        program = tc
    else:
        program = a * tc + (1 - a) * rc
    return clamp(r * program + (1 - r) * ret_norm(pit))


def rewrite(pit: dict, target: float) -> dict:
    """Return a copy whose fields make the APP's own formula compute `target`.
    0.6*(0.7*tc' + 0.3*rec') + 0.4*ret' == target  when tc'=rec'=ret'=target."""
    out = dict(pit)
    out["talent_score"] = 250 + target * 7.5          # inverts tc_norm
    out["recruiting_rank"] = 130 - target * 1.29      # inverts rec_norm
    out["pct_ppa_returning"] = target                 # ret_norm is identity
    return out


# ---------------------------------------------------------------- harness glue
def probe_inputs(pit: dict, key: str) -> float:
    """Recover one input's normalised value through the app's own composite (single weight on)
    — the same trick the rig uses, so the scale is the live app's, not a reimplementation."""
    import app
    prev = os.environ.get("COMPOSITE_WEIGHTS_JSON")
    env = {"sp_plus": 0.0, "efficiency": 0.0, "talent": 0.0, "experience": 0.0}
    env[key] = 1.0
    os.environ["COMPOSITE_WEIGHTS_JSON"] = json.dumps(env)
    try:
        return float(app.project_score_multi_factor(dict(pit), is_home=True).get("composite") or 0.0)
    finally:
        if prev is None:
            os.environ.pop("COMPOSITE_WEIGHTS_JSON", None)
        else:
            os.environ["COMPOSITE_WEIGHTS_JSON"] = prev


def load_season(season: int):
    """Build the game rows once: PIT data, book line, actual result, sp/eff/exp norms."""
    import run_model_backtest as hb
    import app

    pf = CACHE / f"preseason_{season}.json"
    gf = CACHE / f"lines_{season}.json"
    if not (pf.exists() and gf.exists()):
        return []
    pre = json.loads(pf.read_text(encoding="utf-8"))
    pmap = pre if isinstance(pre, dict) else {t["name"]: t for t in pre}
    games = json.loads(gf.read_text(encoding="utf-8"))

    prior = {}
    ppf = CACHE / f"preseason_{season - 1}.json"
    if ppf.exists():
        ppre = json.loads(ppf.read_text(encoding="utf-8"))
        prior = ppre if isinstance(ppre, dict) else {t["name"]: t for t in ppre}

    neutral = {}
    nf = CACHE / f"neutral_{season}.json"
    if nf.exists():
        neutral = json.loads(nf.read_text(encoding="utf-8"))

    # Experience lives in its OWN file (the PIT baselines do not carry it). Without this the
    # experience probe returns a constant 50 for every team, which is inert in a head-to-head
    # gap — the arm deltas would survive, but arm A could not reproduce the published V6.1
    # numbers, and a control that cannot reproduce the baseline is not a control.
    exp_map = {}
    ef = CACHE / f"experience_{season}.json"
    if ef.exists():
        e = json.loads(ef.read_text(encoding="utf-8"))
        exp_map = e if isinstance(e, dict) else {r.get("team"): r for r in e}

    # weekly stats are only needed for weeks>1 in-season refreshes; reuse the harness cache
    try:
        client = app.httpx.Client(timeout=20)
        completed = sorted({int(g.get("week") or 0) for g in games
                            if g.get("homeScore") is not None and g.get("awayScore") is not None})
        past = tuple(range(1, max(completed))) if completed else ()
        weekly = hb.build_weekly_stats_cache(client, weeks=past, year=season) if past else {}
    except Exception as e:  # noqa: BLE001
        print(f"  [{season}] weekly stats unavailable ({e}); week>1 uses the baseline")
        weekly = {}

    rows = []
    for g in games:
        h, a = g.get("homeTeam"), g.get("awayTeam")
        hs, as_ = g.get("homeScore"), g.get("awayScore")
        if hs is None or as_ is None or h not in pmap or a not in pmap:
            continue
        wk = g.get("week") or 1
        hp = hb.reconstruct_pit_team_data(pmap, weekly, h, wk)
        ap = hb.reconstruct_pit_team_data(pmap, weekly, a, wk)
        if not hp or not ap:
            continue
        if not all(p.get("sp_plus") not in (None, 0) for p in (hp, ap)):
            continue                                    # FBS-vs-FBS only, as the rig does
        for pit, nm in ((hp, h), (ap, a)):
            if pit.get("experience_score") is None:
                ex = exp_map.get(nm) or {}
                if isinstance(ex, dict) and ex.get("experience_score") is not None:
                    pit["experience_score"] = ex["experience_score"]
        line = None
        for prov in hb.PROVIDER_PREFERENCE:
            for l in g.get("lines", []):
                if l.get("provider") == prov and l.get("spread") is not None:
                    line = l
                    break
            if line:
                break
        if not line:
            continue
        rows.append({
            "season": season, "week": wk, "home": h, "away": a,
            "hp": hp, "ap": ap,
            "prior_rec": {k: (prior.get(k) or {}).get("recruiting_rank") for k in (h, a)},
            # sp/eff/exp are VARIANT-INDEPENDENT (only the talent blend changes), so probe them
            # once here rather than per arm — the talent term is known by construction in score().
            "h_see": (probe_inputs(hp, "sp_plus"), probe_inputs(hp, "efficiency"), probe_inputs(hp, "experience")),
            "a_see": (probe_inputs(ap, "sp_plus"), probe_inputs(ap, "efficiency"), probe_inputs(ap, "experience")),
            "book_home_margin": -float(line["spread"]),
            "actual_home_margin": hs - as_,
            "neutral": bool(neutral.get(str(g.get("id"))) or neutral.get(g.get("id"))),
        })
    return rows


def score(rows, variant, slope=SLOPE, hfa=HFA, weights=None, decay=None, wk_filter=None):
    """Model margin per arm variant; returns MAE vs book, bias, mean|margin| and ATS record.

    decay = floor fraction for the talent weight: talent_w(w) = T0 * max(floor, 1-(w-1)/12),
    and the freed weight moves to EFFICIENCY — i.e. as real offensive/defensive production
    accumulates, a static roster prior should matter less. The fresh weight goes to efficiency
    rather than SP+ because efficiency is the term that actually refreshes week to week."""
    import app
    Wt = weights or W
    errs, signed, margins = [], [], []
    errs_actual, signed_actual = [], []
    ranks_model, ranks_actual = [], []
    ats = {t: {"w": 0, "l": 0, "p": 0} for t in (0.5, 3.5, 7.0)}   # all picks / 3.5pt / 7pt edges
    for r in rows:
        if wk_filter and not wk_filter(r["week"]):
            continue
        hp, ap = dict(r["hp"]), dict(r["ap"])
        if variant == "B":
            for side, pit in (("hp", hp), ("ap", ap)):
                pr = r["prior_rec"].get(r["home"] if side == "hp" else r["away"])
                if pr:
                    pit["recruiting_rank"] = pr
        # Target talent_norm for this arm. `rewrite()` makes the APP compute exactly this value,
        # so the talent contribution is W['talent'] * target with no probe call needed.
        if variant.startswith("D:"):
            aa, rr = float(variant.split(":")[1]), float(variant.split(":")[2])
            th, ta = talent_blend(hp, aa, rr), talent_blend(ap, aa, rr)
        elif variant == "C":
            th, ta = talent_blend(hp, a=1.0), talent_blend(ap, a=1.0)
        else:  # A and B (B's lag is applied to hp/ap above)
            th, ta = talent_blend(hp), talent_blend(ap)
        sh, eh, xh = r["h_see"]
        sa, ea, xa = r["a_see"]
        if decay is None:
            w_sp, w_eff, w_exp, w_tal = Wt["sp_plus"], Wt["efficiency"], Wt["experience"], Wt["talent"]
        else:
            # decay is (floor, start_week): the prior is held INTACT through start_week and only
            # then walks down to `floor` by week 13. Starting the walk at week 1 charges early
            # weeks for a decay they cannot earn — there is no production to hand the weight to.
            floor, start, end = (decay if isinstance(decay, tuple) and len(decay) == 3
                                 else (float(decay), 1, 13) if not isinstance(decay, tuple)
                                 else (decay[0], decay[1], 13))
            wk = r["week"] or 1
            frac = 1.0 if wk <= start else max(float(floor),
                                              1.0 - (wk - start) / max(1.0, end - start))
            w_tal = Wt["talent"] * frac
            w_eff = Wt["efficiency"] + (Wt["talent"] - w_tal)
            w_sp, w_exp = Wt["sp_plus"], Wt["experience"]
        ch = sh * w_sp + eh * w_eff + xh * w_exp + th * w_tal
        ca = sa * w_sp + ea * w_eff + xa * w_exp + ta * w_tal
        gap = ch - ca
        m = (1 if gap >= 0 else -1) * abs(gap) ** POWER * slope + (0.0 if r["neutral"] else hfa)
        book = r["book_home_margin"]
        errs.append(abs(m - book))
        signed.append(m - book)
        margins.append(abs(m))
        # POWER-RANKING metric (Jeff's stated goal): how well does this grade the teams, measured
        # against the ACTUAL final margin rather than the market's opinion. MAE-vs-book measures
        # betability; MAE-vs-actual measures whether the team grades are right.
        errs_actual.append(abs(m - r["actual_home_margin"]))
        signed_actual.append(m - r["actual_home_margin"])
        ranks_model.append(m)
        ranks_actual.append(r["actual_home_margin"])
        edge = abs(m - book)
        pick_home = m > book
        cover = r["actual_home_margin"] - book
        for thr, d in ats.items():
            if edge < thr:
                continue
            if cover == 0:
                d["p"] += 1
            elif (pick_home and cover > 0) or (not pick_home and cover < 0):
                d["w"] += 1
            else:
                d["l"] += 1
    n = len(errs)
    def pct(d):
        dec = d["w"] + d["l"]
        return (100.0 * d["w"] / dec) if dec else 0.0
    # Scale-free power-ranking measures: the compressed scale (mean |margin| ~10 vs actual ~14)
    # dilutes MAE-vs-actual, so also measure ORDERING quality directly.
    su_num = su_den = 0
    for m, act in zip(ranks_model, ranks_actual):
        if act == 0 or m == 0:
            continue
        su_den += 1
        if (m > 0) == (act > 0):
            su_num += 1
    def _rank(vals):
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        rk = [0.0] * len(vals)
        for pos, i in enumerate(order):
            rk[i] = float(pos)
        return rk
    rho = 0.0
    if len(ranks_model) > 2:
        a, b_ = _rank(ranks_model), _rank(ranks_actual)
        ma, mb = sum(a) / len(a), sum(b_) / len(b_)
        cov = sum((x - ma) * (y - mb) for x, y in zip(a, b_))
        va = sum((x - ma) ** 2 for x in a) ** 0.5
        vb = sum((y - mb) ** 2 for y in b_) ** 0.5
        rho = (cov / (va * vb)) if va and vb else 0.0
    return {
        "su_acc": (100.0 * su_num / su_den) if su_den else 0.0,
        "spearman": rho,
        "n": n, "mae": sum(errs) / n if n else 0.0,
        "bias": sum(signed) / n if n else 0.0,
        "mean_abs_margin": sum(margins) / n if n else 0.0,
        "mae_actual": sum(errs_actual) / n if n else 0.0,
        "bias_actual": sum(signed_actual) / n if n else 0.0,
        "ats_pct": pct(ats[0.5]), "ats_decisions": ats[0.5]["w"] + ats[0.5]["l"],
        "ats_3p5_pct": pct(ats[3.5]), "ats_3p5_decisions": ats[3.5]["w"] + ats[3.5]["l"],
        "ats_7p0_pct": pct(ats[7.0]), "ats_7p0_decisions": ats[7.0]["w"] + ats[7.0]["l"],
    }


FLOORS = [(1.0, 1, 13), (0.5, 4, 9), (0.5, 1, 8), (0.55, 1, 8), (0.65, 1, 8), (0.75, 1, 8)]
# (1.0, 1, 13) == no decay at all, i.e. the base — so the fit is free to REJECT decay entirely.
# FIXED is Jeff's preferred shape (start wk1, bottom wk8, higher floor), evaluated on EVERY fold
# rather than only when the in-sample fit happens to choose it.
FIXED = (0.65, 1, 8)

# Named shapes evaluated on EVERY fold (not just when the in-sample fit picks them), so the
# comparison is like-for-like across seasons.
SHAPES = {
    "base (no decay)":         (1.0, 1, 13),
    "wk1->wk8 floor0.65":      (0.65, 1, 8),
    "wk4->wk9 floor0.50":      (0.50, 4, 9),
    "wk5->wk9 floor0.50":      (0.50, 5, 9),
    "wk4->wk9 floor0.65":      (0.65, 4, 9),
    "wk5->wk9 floor0.65":      (0.65, 5, 9),
    "late-only wk6 floor0.50": (0.50, 6, 9),
}


def run_gate(seasons, hfa_values):
    """Full stability gate for the week-decay arm.
    Gate (per the add-on rule): interior optimum, non-flat MAE surface, no ATS degradation at
    the MAE-optimal setting, and it must pass on BOTH base variants (HFA 3.50 and 4.157).
    Leave-one-season-out is used because a single holdout is not enough for a flexible model."""
    data = {s: load_season(s) for s in seasons}
    all_rows = [r for s in seasons for r in data[s]]
    print(f"loaded {len(all_rows)} FBS games across {seasons}")

    for hfa in hfa_values:
        print(f"\n=== BASE HFA {hfa:.3f} ===  (* floor fitted by MAE on the other seasons)")
        print(f"{'holdout':<9}{'floor*':>7}{'base MAE':>10}{'arm MAE':>9}{'dMAE':>8}"
              f"{'base ATS':>10}{'arm ATS':>9}{'dATS':>7}{'b3.5':>7}{'a3.5':>7}{'b7':>7}{'a7':>7}")
        dmae, dats, wins = [], [], 0
        fdmae, fdats, fwins = [], [], 0
        for S in seasons:
            fit = [r for s in seasons if s != S for r in data[s]]
            hold = data[S]
            curve = {f: score(fit, "A", hfa=hfa, decay=f)["mae"] for f in FLOORS}
            best_f = min(curve, key=curve.get)
            b = score(hold, "A", hfa=hfa)
            a = score(hold, "A", hfa=hfa, decay=best_f)
            fx = score(hold, "A", hfa=hfa, decay=FIXED)
            dmae.append(a["mae"] - b["mae"])
            dats.append(a["ats_pct"] - b["ats_pct"])
            fdmae.append(fx["mae"] - b["mae"])
            fdats.append(fx["ats_pct"] - b["ats_pct"])
            wins += 1 if a["mae"] < b["mae"] else 0
            fwins += 1 if fx["mae"] < b["mae"] else 0
            print(f"{S:<9}{str(best_f):>13}{b['mae']:>10.3f}{a['mae']:>9.3f}{a['mae']-b['mae']:>+8.3f}"
                  f"{b['ats_pct']:>9.1f}%{a['ats_pct']:>8.1f}%{a['ats_pct']-b['ats_pct']:>+6.1f}"
                  f"{b['ats_3p5_pct']:>7.1f}{a['ats_3p5_pct']:>7.1f}"
                  f"{b['ats_7p0_pct']:>7.1f}{a['ats_7p0_pct']:>7.1f}"
                  f"   | FIXED {FIXED}: dMAE {fx['mae']-b['mae']:+.3f} dATS {fx['ats_pct']-b['ats_pct']:+.1f}pp")
        print(f"  MAE-fitted arm: mean dMAE {sum(dmae)/len(dmae):+.3f} | mean dATS {sum(dats)/len(dats):+.2f}pp | "
              f"better on {wins}/{len(seasons)} folds")
        print(f"  FIXED {FIXED}:   mean dMAE {sum(fdmae)/len(fdmae):+.3f} | mean dATS {sum(fdats)/len(fdats):+.2f}pp | "
              f"better on {fwins}/{len(seasons)} folds")
        print("  MAE surface across the floor grid (pooled fit): "
              + "  ".join(f"{f}:{score(all_rows, 'A', hfa=hfa, decay=f)['mae']:.3f}"
                          + ("*edge" if f in (0.0, 1.0) else "") for f in FLOORS))

        # Like-for-like: every named shape scored on every held-out season.
        acc = {n: {"d": [], "a": [], "wins": 0} for n in SHAPES}
        per_fold = {n: [] for n in SHAPES}
        for S in seasons:
            hold = data[S]
            b = score(hold, "A", hfa=hfa)
            for nm, sh in SHAPES.items():
                s = score(hold, "A", hfa=hfa, decay=sh)
                acc[nm]["d"].append(s["mae"] - b["mae"])
                acc[nm]["a"].append(s["ats_pct"] - b["ats_pct"])
                acc[nm].setdefault("da", []).append(s["mae_actual"] - b["mae_actual"])
                acc[nm].setdefault("su", []).append(s["su_acc"] - b["su_acc"])
                acc[nm].setdefault("rho", []).append(s["spearman"] - b["spearman"])
                acc[nm]["wins"] += 1 if s["mae"] < b["mae"] else 0
                per_fold[nm].append(s["ats_pct"] - b["ats_pct"])
        print(f"\n  --- LOSO per SHAPE (holdouts {seasons}) ---")
        print(f"  {'shape':<26}{'dMAE book':>11}{'dMAE ACT':>10}{'dSU acc':>9}{'dSpearman':>11}{'dATS':>9}")
        for nm in SHAPES:
            v = acc[nm]
            print(f"  {nm:<26}{sum(v['d'])/len(v['d']):>+11.3f}"
                  f"{sum(v['da'])/len(v['da']):>+10.3f}"
                  f"{sum(v['su'])/len(v['su']):>+8.2f}pp"
                  f"{sum(v['rho'])/len(v['rho']):>+11.4f}"
                  f"{sum(v['a'])/len(v['a']):>+8.2f}pp")
        pooled_a = score(all_rows, "A", hfa=hfa, decay=0.5)
        pooled_b = score(all_rows, "A", hfa=hfa)
        print(f"  POOLED all seasons @floor 0.50: base MAE {pooled_b['mae']:.3f} ATS {pooled_b['ats_pct']:.1f}%"
              f" | arm MAE {pooled_a['mae']:.3f} ATS {pooled_a['ats_pct']:.1f}%"
              f" (dMAE {pooled_a['mae']-pooled_b['mae']:+.3f}, dATS {pooled_a['ats_pct']-pooled_b['ats_pct']:+.1f}pp)")
    return 0


def main() -> int:
    ap_ = argparse.ArgumentParser()
    ap_.add_argument("--seasons", type=int, nargs="+", default=[2022, 2023, 2024])
    ap_.add_argument("--holdout", type=int, default=2025)
    ap_.add_argument("--gate", action="store_true",
                     help="run the stability gate: LOSO over all seasons x both HFA bases")
    args = ap_.parse_args()

    if args.gate:
        return run_gate([2021, 2022, 2023, 2024, 2025], [3.50, 4.157])

    seasons = list(args.seasons) + [args.holdout]
    data = {}
    for s in seasons:
        rows = load_season(s)
        data[s] = rows
        print(f"loaded {s}: {len(rows)} FBS games with a book line")

    fit_rows = [r for s in args.seasons for r in data[s]]
    hold_rows = data[args.holdout]
    print(f"\nfit n={len(fit_rows)}  holdout n={len(hold_rows)}")

    variants = ["A", "B", "C"]
    variants += [f"D:{a}:{r}" for a, r in itertools.product([0.0, 0.25, 0.5, 0.7, 1.0], [0.4, 0.6, 0.8])]
    # TOP-LEVEL talent weight sweep — the blend above barely moves the board; the SIZE of the
    # talent weight is what puts a roster/recruiting signal at 0.32 of the composite. Freed
    # weight is redistributed across sp_plus/efficiency/experience in proportion to their
    # current shares, so the arm stays a single-variable change.
    variants += ["T:0.05", "T:0.10", "T:0.15", "T:0.20", "T:0.25"]
    # W arms: talent weight DECAYS through the season (T0 x max(floor, 1-(w-1)/12)), the freed
    # weight going to in-season efficiency. This is Jeff's proposal: a static roster prior should
    # matter less as real offensive/defensive production accumulates.
    variants += ["W:0.50", "W:0.25", "W:0.00"]
    # S arms: DELAYED decay — hold the prior intact through week `start`, then walk to the floor
    # by week 13. Tested because starting the walk at week 1 charges weeks 1-4 for a decay they
    # cannot earn (no production exists yet to receive the weight).
    variants += ["S:0.50:4:13", "S:0.50:4:9", "S:0.55:1:8", "S:0.65:1:8", "S:0.75:1:8",
                 "S:0.50:1:8", "S:0.25:4:9", "S:0.10:5:9"]
    decay_sets = {}
    for v in variants:
        if v.startswith("W:"):
            decay_sets[v] = float(v.split(":")[1])
        elif v.startswith("S:"):
            parts = v.split(":")
            decay_sets[v] = (float(parts[1]), int(parts[2]),
                             int(parts[3]) if len(parts) > 3 else 13)
    weight_sets = {}
    for v in variants:
        if v.startswith("T:"):
            w = float(v.split(":")[1])
            rest = 1.0 - w
            base_rest = W["sp_plus"] + W["efficiency"] + W["experience"]
            weight_sets[v] = {"sp_plus": rest * W["sp_plus"] / base_rest,
                              "efficiency": rest * W["efficiency"] / base_rest,
                              "experience": rest * W["experience"] / base_rest,
                              "talent": w}

    print(f"\n{'variant':<12}{'fit MAE':>9}{'fit bias':>10}{'|m|':>7}   {'hold MAE':>9}{'hold ATS':>10}{'n':>7}")
    results = {}
    for v in variants:
        f = score(fit_rows, v, weights=weight_sets.get(v), decay=decay_sets.get(v))
        h = score(hold_rows, v, weights=weight_sets.get(v), decay=decay_sets.get(v))
        results[v] = {"fit": f, "hold": h}
        print(f"{v:<12}{f['mae']:>9.3f}{f['bias']:>10.3f}{f['mean_abs_margin']:>7.2f}   "
              f"{h['mae']:>9.3f}{h['ats_pct']:>9.1f}%{h['n']:>7}")

    base = results["A"]
    print("\n--- vs base (fit MAE, holdout MAE, holdout ATS) ---")
    for v, r in results.items():
        if v == "A":
            continue
        print(f"  {v:<12} fit MAE {r['fit']['mae'] - base['fit']['mae']:+.3f} | "
              f"hold MAE {r['hold']['mae'] - base['hold']['mae']:+.3f} | "
              f"hold ATS {r['hold']['ats_pct'] - base['hold']['ats_pct']:+.1f}pp")

    print("\n--- EARLY (wk 1-4) vs LATE (wk 8+): a decay arm must earn its keep in the LATE weeks ---")
    print(f"{'variant':<10}{'early MAE':>11}{'early ATS':>11}{'late MAE':>10}{'late ATS':>10}{'late n':>8}")
    early = lambda w: (w or 1) <= 4
    late = lambda w: (w or 1) >= 8
    for v in ["A"] + [x for x in variants if x.startswith(("W:", "S:"))]:
        fe = score(fit_rows, v, decay=decay_sets.get(v), wk_filter=early)
        fl = score(fit_rows, v, decay=decay_sets.get(v), wk_filter=late)
        print(f"{v:<10}{fe['mae']:>11.3f}{fe['ats_pct']:>10.1f}%{fl['mae']:>10.3f}"
              f"{fl['ats_pct']:>9.1f}%{fl['n']:>8}")

    out = ROOT / "data" / "backtest_study" / "talent_arms.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"seasons": seasons, "holdout": args.holdout,
                               "slope": SLOPE, "hfa": HFA, "power": POWER,
                               "results": results}, indent=2), encoding="utf-8")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())