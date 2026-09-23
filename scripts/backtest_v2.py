#!/usr/bin/env python3
"""backtest_v2.py — MODEL V2 (two-level design).

LEVEL 1 (Team Composite, 0-100): SP+ 35 / Efficiency 30 / Talent 20 / Experience 15.
  Dropped: SRS (proven inert), Elo, FPI.
LEVEL 2 (game adjustment, margin points): havoc, explosiveness, rest, wind.
  margin = (comp_home - comp_away) * slope + game_adj + HFA

Normalisation is NOT reimplemented: the production composite is a pure weighted sum, so setting a
single weight to 1.0 makes production's composite equal that input's normalised value. Three probe
configs recover sp_norm / eff_norm / talent_norm from app.py. Experience comes from
fetch_season_experience.py, havoc from fetch_season_havoc.py.

INJURY: re-specified as a COMPOSITE DISCOUNT (Level 1), approved by CEO/Jeff — when the QB is out
the TEAM is worse, not the matchup. It is NOT implemented for 2021-2025 because no historical
injury data exists (active_injuries.json is a single live snapshot). It can only be validated on
the current season. Its magnitudes are judgment, not measured — mark them so.

WIND: not yet wired (needs a /games/weather fetch). Totals-only effect, >=15 mph sustained.

HAVOC SOURCE: prior season (leak-free). In-season havoc has no point-in-time source — D1 stores
the components at week 0 only — so the play-by-play harvest is required for a weekly version.

Usage:
  python scripts/backtest_v2.py --seasons 2021 2022 2023 2024 2025 --holdout 2025
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from backtest_study import build_records, evaluate, fmt, objective  # noqa: E402

CACHE = ROOT / "data" / "backtest_cache"
OUT = ROOT / "data" / "backtest_study"

V2_W = {"sp": 0.35, "eff": 0.30, "tal": 0.20, "exp": 0.15}
V2_W_NOEXP = {"sp": 0.35 / 0.85, "eff": 0.30 / 0.85, "tal": 0.20 / 0.85, "exp": 0.0}

HFA = 2.5
SLOPE_GRID = [round(0.5 + 0.1 * i, 2) for i in range(11)]     # 0.5 .. 1.5
K_GRID = [round(0.05 * i, 2) for i in range(21)]              # 0.0 .. 1.0
HAVOC_GRID = [0.0, 1.5, 3.0, 4.5]                             # spec default 3.0
EXPL_GRID = [0.0, 3.0, 6.0, 9.0]                              # spec default 6.0
REST_GRID = [0.0, 0.15, 0.3, 0.45]                            # spec default 0.3 pts/day

PROBE = {
    "sp":  {"sp_plus": 1.0, "fpi": 0.0, "srs": 0.0, "elo": 0.0, "talent": 0.0, "efficiency": 0.0},
    "eff": {"sp_plus": 0.0, "fpi": 0.0, "srs": 0.0, "elo": 0.0, "talent": 0.0, "efficiency": 1.0},
    "tal": {"sp_plus": 0.0, "fpi": 0.0, "srs": 0.0, "elo": 0.0, "talent": 1.0, "efficiency": 0.0},
}
SPEC = {"slope": 1.0, "havoc": 3.0, "expl": 6.0, "rest": 0.3}


def _api_key() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("CFBD_API_KEY"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("CFBD_API_KEY not found")


def neutral_sites(season: int) -> dict:
    """gameId -> neutralSite; the lines cache has no venue flag, so HFA would be wrongly
    applied to neutral-site games without this join."""
    fp = CACHE / f"neutral_{season}.json"
    if fp.exists():
        return json.loads(fp.read_text(encoding="utf-8"))
    url = f"https://api.collegefootballdata.com/games?year={season}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {_api_key()}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        games = json.load(r)
    out = {str(g["id"]): bool(g.get("neutralSite")) for g in games}
    fp.write_text(json.dumps(out), encoding="utf-8")
    return out


def pit_explosiveness(season: int) -> dict:
    """{(team, week): (off_expl, def_expl)} — cumulative mean through week-1, point-in-time.

    The weekly advanced cache carries offense/defense explosiveness per team-week, but the
    reconstructed PIT team dict does NOT, so it has to be accumulated here.
    """
    files = sorted(CACHE.glob(f"stats_game_advanced_{season}_wk*.json"),
                   key=lambda p: int(p.stem.split("_wk")[-1]))
    cum: dict[str, list] = {}
    out: dict[tuple, tuple] = {}
    for fp in files:
        wk = int(fp.stem.split("_wk")[-1])
        rows = json.loads(fp.read_text(encoding="utf-8"))
        # snapshot the state BEFORE this week's games -> point-in-time
        for team, (o, d, n) in cum.items():
            out[(team, wk)] = (o / n, d / n)
        for r in rows:
            t = r.get("team")
            off = (r.get("offense") or {}).get("explosiveness")
            dfn = (r.get("defense") or {}).get("explosiveness")
            if t and off is not None and dfn is not None:
                o, d, n = cum.get(t, (0.0, 0.0, 0))
                cum[t] = (o + off, d + dfn, n + 1)
    return out


def probe_norm(recs: list[dict], key: str) -> None:
    import app
    prev = os.environ.get("COMPOSITE_WEIGHTS_JSON")
    os.environ["COMPOSITE_WEIGHTS_JSON"] = json.dumps(PROBE[key])
    try:
        for r in recs:
            h2h = app.project_head_to_head(r["home_pit"], r["away_pit"])
            r[f"{key}_h"] = h2h["home_composite"]
            r[f"{key}_a"] = h2h["away_composite"]
    finally:
        if prev is None:
            os.environ.pop("COMPOSITE_WEIGHTS_JSON", None)
        else:
            os.environ["COMPOSITE_WEIGHTS_JSON"] = prev


def v2_composite(r: dict, side: str, w: dict) -> float:
    exp = r.get(f"exp_{side}")
    exp = 50.0 if exp is None else exp
    return (w["sp"] * r[f"sp_{side}"] + w["eff"] * r[f"eff_{side}"]
            + w["tal"] * r[f"tal_{side}"] + w["exp"] * exp)


def game_adj(r: dict, p: dict) -> float:
    """Level 2: havoc + explosiveness + rest, in margin points (spec clamps)."""
    adj = 0.0
    dh, ha = r.get("hv_h"), r.get("hva_a")     # home def_havoc, away havoc_allowed
    da, hh = r.get("hv_a"), r.get("hva_h")
    if None not in (dh, ha, da, hh):
        net = (dh - ha) - (da - hh)
        adj += max(-1.0, min(1.0, net)) * p["havoc"]
    eh, ea = r.get("expl_off_h"), r.get("expl_def_a")
    ea2, eh2 = r.get("expl_off_a"), r.get("expl_def_h")
    if None not in (eh, ea, ea2, eh2):
        net = (eh - ea) - (ea2 - eh2)
        adj += max(-0.5, min(0.5, net)) * p["expl"]
    rh, ra = r.get("rest_home"), r.get("rest_away")
    if rh is not None and ra is not None:
        adj += max(-2.0, min(2.0, (rh - ra) * p["rest"]))
    return adj


def apply_arm(recs: list[dict], w: dict, mode: str, param: float, p: dict, use_adj=True) -> None:
    """Write the arm's margin/total into the fields backtest_study.evaluate() reads."""
    for r in recs:
        avg = (v2_composite(r, "h", w) + v2_composite(r, "a", w)) / 2.0
        r["model_total"] = round(51.0 + (avg - 50.0) * 0.10, 1)
        r["edge"] = v2_composite(r, "h", w) - v2_composite(r, "a", w)
        hfa = 0.0 if r["neutral"] else HFA
        adj = game_adj(r, p) if use_adj else 0.0
        if mode == "market":
            r["model_margin"] = r["edge"] * param   # HFA deliberately NOT re-applied
        else:
            r["model_margin"] = r["edge"] * param + adj + hfa


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", type=int, nargs="+", required=True)
    ap.add_argument("--holdout", type=int, default=2025)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    recs: list[dict] = []
    for s in args.seasons:
        rs = build_records(s, None)
        ns = neutral_sites(s)
        expl = pit_explosiveness(s)
        hv = json.loads((CACHE / f"havoc_{s - 1}.json").read_text(encoding="utf-8")) \
            if (CACHE / f"havoc_{s - 1}.json").exists() else {}
        exp = json.loads((CACHE / f"experience_{s}.json").read_text(encoding="utf-8")) \
            if (CACHE / f"experience_{s}.json").exists() else {}
        cov = 0
        for r in rs:
            r["neutral"] = ns.get(str(r["game_id"]), False)
            h, a, wk = r["home"], r["away"], r["week"]
            r["exp_h"] = (exp.get(h) or {}).get("experience_score")
            r["exp_a"] = (exp.get(a) or {}).get("experience_score")
            hh, aa = hv.get(h) or {}, hv.get(a) or {}
            r["hv_h"], r["hva_h"] = hh.get("def_havoc"), hh.get("havoc_allowed")
            r["hv_a"], r["hva_a"] = aa.get("def_havoc"), aa.get("havoc_allowed")
            po, pd = expl.get((h, wk)), expl.get((a, wk))
            r["expl_off_h"], r["expl_def_h"] = (po if po else (None, None))
            r["expl_off_a"], r["expl_def_a"] = (pd if pd else (None, None))
            if r["hv_h"] is not None and r["expl_off_h"] is not None:
                cov += 1
        recs.extend(rs)
        print(f"[v2] {s}: {len(rs)} games, {sum(1 for r in rs if r['neutral'])} neutral, "
              f"Level-2 coverage {cov}/{len(rs)}")

    print("[v2] recovering production normalisations (3 probe passes)")
    for key in PROBE:
        probe_norm(recs, key)

    fit_seasons = [s for s in args.seasons if s != args.holdout]
    report = {"seasons": args.seasons, "holdout": args.holdout, "model": "v2", "arms": {}}

    def measure(w, mode, param, p, use_adj=True, seasons=None):
        apply_arm(recs, w, mode, param, p, use_adj)
        if mode == "market":
            # MUST go through the study's market formula so the OPENING LINE is the prior.
            # Reading model_margin here would silently drop the line and report a
            # compressed composite model under a market-anchor label.
            return evaluate(recs, {"margin": "market", "prior": "open"}, param, 0.0, seasons)
        return evaluate(recs, {"margin": "model"}, seasons=seasons)

    def record(name, w, mode, param, p, use_adj=True, extra=None):
        pooled = fmt(measure(w, mode, param, p, use_adj))
        hold = fmt(measure(w, mode, param, p, use_adj, [args.holdout]))
        per = {str(s): fmt(measure(w, mode, param, p, use_adj, [s])) for s in args.seasons}
        report["arms"][name] = {"pooled": pooled, "per_season": per,
                                "params": {"slope_or_k": param, **p}, "extra": extra or {},
                                "holdout_ats": hold["ats_fbs"],
                                "holdout_ats_pct": hold["ats_fbs_pct"],
                                "holdout_games": hold["fbs_games"]}
        print(f"[v2] {name:<16} ATS-FBS {pooled['ats_fbs_pct']}% ({pooled['ats_fbs']})  "
              f"O/U {pooled['ou_pct']}%  SU {pooled['su_pct']}%  MAE {pooled['mae_vs_book']}  "
              f"|margin| {pooled['mean_model_margin']} vs book {pooled['mean_book_line']}  "
              f"| held-out {args.holdout}: {hold['ats_fbs_pct']}%")

    # ---- Level 1 only / minus experience (already informative) ----
    for name, w in (("v2-no-matchup", V2_W), ("v2-no-experience", V2_W_NOEXP)):
        best = (-1.0, SLOPE_GRID[0])
        for sl in SLOPE_GRID:
            v = objective(measure(w, "model", sl, SPEC, use_adj=False, seasons=fit_seasons))
            if v > best[0]:
                best = (v, sl)
        record(name, w, "model", best[1], SPEC, use_adj=False,
               extra={"in_sample_ats_pct": round(100 * best[0], 1)})

    # ---- v2-baseline: Level 1 + Level 2 ----
    best = (-1.0, SPEC["slope"])
    for sl in SLOPE_GRID:
        v = objective(measure(V2_W, "model", sl, SPEC, seasons=fit_seasons))
        if v > best[0]:
            best = (v, sl)
    record("v2-baseline", V2_W, "model", best[1], SPEC,
           extra={"in_sample_ats_pct": round(100 * best[0], 1)})

    # ---- v2-market-open: spec form (no game_adj, HFA not re-applied) ----
    best = (-1.0, K_GRID[0])
    for k in K_GRID:
        v = objective(measure(V2_W, "market", k, SPEC, seasons=fit_seasons))
        if v > best[0]:
            best = (v, k)
    ref0 = fmt(measure(V2_W, "market", 0.0, SPEC, seasons=fit_seasons))
    record("v2-market-open", V2_W, "market", best[1], SPEC,
           extra={"in_sample_ats_pct": round(100 * best[0], 1), "note": "spec form: no game_adj",
                  "reference_k0_ats": ref0["ats_fbs"], "reference_k0_pct": ref0["ats_fbs_pct"],
                  "reference_note": "k=0.0 = pure opening line, no composite contribution"})

    # ---- refit-v2: bounded coordinate descent (slope + L2 scales + L1 weights) ----
    w = dict(V2_W)
    p = dict(SPEC)
    slope = 1.0
    for _ in range(2):
        for sl in SLOPE_GRID:
            if objective(measure(w, "model", sl, p, seasons=fit_seasons)) > \
               objective(measure(w, "model", slope, p, seasons=fit_seasons)):
                slope = sl
        for key, grid in (("havoc", HAVOC_GRID), ("expl", EXPL_GRID), ("rest", REST_GRID)):
            cur = p[key]
            for g in grid:
                trial = dict(p); trial[key] = g
                if objective(measure(w, "model", slope, trial, seasons=fit_seasons)) > \
                   objective(measure(w, "model", slope, p, seasons=fit_seasons)):
                    p = trial
            p[key] = p[key] if p[key] != cur else cur
        for key, grid in (("sp", [0.25, 0.30, 0.35, 0.40]), ("eff", [0.20, 0.25, 0.30, 0.35]),
                          ("tal", [0.10, 0.15, 0.20, 0.25]), ("exp", [0.05, 0.10, 0.15, 0.20])):
            for g in grid:
                trial = dict(w); trial[key] = g
                tot = trial["sp"] + trial["eff"] + trial["tal"] + trial["exp"]
                if tot <= 0 or abs(tot - 1.0) > 0.005:
                    continue
                if objective(measure(trial, "model", slope, p, seasons=fit_seasons)) > \
                   objective(measure(w, "model", slope, p, seasons=fit_seasons)):
                    w = trial
    wsum = sum(w.values())
    w = {k: round(v / wsum, 6) for k, v in w.items()}
    record("refit-v2", w, "model", slope, p,
           extra={"fitted_weights": w, "note": "bounded coordinate descent, 2 passes; "
                                                "not an exhaustive simplex search"})

    dest = Path(args.out) if args.out else OUT / f"v2_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    dest.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"[v2] report -> {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())