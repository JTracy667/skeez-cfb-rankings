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


def score(rows, variant, slope=SLOPE, hfa=HFA, weights=None):
    """Model margin per arm variant; returns MAE vs book, bias, mean|margin| and ATS record."""
    import app
    Wt = weights or W
    errs, signed, margins, ats = [], [], [], {"w": 0, "l": 0, "p": 0}
    for r in rows:
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
        ch = sh * Wt["sp_plus"] + eh * Wt["efficiency"] + xh * Wt["experience"] + th * Wt["talent"]
        ca = sa * Wt["sp_plus"] + ea * Wt["efficiency"] + xa * Wt["experience"] + ta * Wt["talent"]
        gap = ch - ca
        m = (1 if gap >= 0 else -1) * abs(gap) ** POWER * slope + (0.0 if r["neutral"] else hfa)
        book = r["book_home_margin"]
        errs.append(abs(m - book))
        signed.append(m - book)
        margins.append(abs(m))
        edge = abs(m - book)
        if edge >= 0.5:
            pick_home = m > book
            cover = r["actual_home_margin"] - book
            if cover == 0:
                ats["p"] += 1
            elif (pick_home and cover > 0) or (not pick_home and cover < 0):
                ats["w"] += 1
            else:
                ats["l"] += 1
    n = len(errs)
    dec = ats["w"] + ats["l"]
    return {
        "n": n, "mae": sum(errs) / n if n else 0.0,
        "bias": sum(signed) / n if n else 0.0,
        "mean_abs_margin": sum(margins) / n if n else 0.0,
        "ats_pct": (100.0 * ats["w"] / dec) if dec else 0.0,
        "ats_decisions": dec, **{f"ats_{k}": v for k, v in ats.items()},
    }


def main() -> int:
    ap_ = argparse.ArgumentParser()
    ap_.add_argument("--seasons", type=int, nargs="+", default=[2022, 2023, 2024])
    ap_.add_argument("--holdout", type=int, default=2025)
    args = ap_.parse_args()

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
        f = score(fit_rows, v, weights=weight_sets.get(v))
        h = score(hold_rows, v, weights=weight_sets.get(v))
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

    out = ROOT / "data" / "backtest_study" / "talent_arms.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"seasons": seasons, "holdout": args.holdout,
                               "slope": SLOPE, "hfa": HFA, "power": POWER,
                               "results": results}, indent=2), encoding="utf-8")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())