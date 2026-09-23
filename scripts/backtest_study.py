#!/usr/bin/env python3
"""backtest_study.py — run a SET of arms over multiple seasons on frozen inputs.

Why this exists separately from run_model_backtest.py: that harness answers "how did the
model do in season Y". A study answers "which candidate is better", which needs several arms
evaluated on identical records, pooled across seasons, split by opponent class, and — for arms
with fitted parameters — fitted on one set of seasons and scored on another.

Design:
  Phase A  for each (weights config, season): build per-game records with the production
           composite (so the composite math is app.py's, not a reimplementation) plus the
           book's lines.  This is the only expensive part.
  Phase B  evaluate each arm's FORMULA over those records. Because the composite edge is
           already in the records, a parameter grid costs arithmetic, not another model pass.

Arms are data (ARMS below). Margin formulas:
  model        margin = production formula (composite gap + HFA etc.) — arms 1-5, 7
  market       margin = prior_line + scaling * composite_edge — arms 6, 8
               prior = opening line (valid) or closing line (leaky comparator, for diagnosis)
  rest         adds (rest_home - rest_away) * rest_factor to whatever the margin formula gives

Usage:
    python scripts/backtest_study.py --seasons 2021 2022 2023 2024 2025
    python scripts/backtest_study.py --seasons 2025 --arms baseline,market-anchor-open
    python scripts/backtest_study.py --seasons 2021 2022 2023 2024 2025 --fit market-anchor-open
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT))

DATA = ROOT / "data"
CACHE = DATA / "backtest_cache"
OUT = DATA / "backtest_study"

# MUST match run_model_backtest.py:39 — this study has to select the same line as the
# published baseline or its numbers are not comparable to it.
PROVIDER_PREFERENCE = ("DraftKings", "Draft Kings", "Bovada")

# ---------------------------------------------------------------------------------------
# Arms are DATA. weights=None means the live defaults.
# ---------------------------------------------------------------------------------------
W_NO_SRS = {"sp_plus": 0.204545, "fpi": 0.170455, "srs": 0.0, "elo": 0.090909,
            "talent": 0.113636, "efficiency": 0.420455}
# srs pinned at 0.05; the other 0.95 re-normalised across the remaining five in proportion to
# their original shares (0.18/0.15/0.08/0.10/0.37 over their 0.88 total).
W_LOW_SRS = {"sp_plus": 0.194318, "fpi": 0.161932, "srs": 0.05, "elo": 0.086364,
             "talent": 0.107955, "efficiency": 0.399432}

# Fail fast: a weight set that does not total 1.0 silently mis-calibrates the 0-100 composite
# and produces confident garbage. The rig validated its arms file; this study must too.
_ENABLED = ("sp_plus", "fpi", "srs", "elo", "talent", "efficiency")
for _n, _w in (("W_NO_SRS", W_NO_SRS), ("W_LOW_SRS", W_LOW_SRS)):
    _t = sum(_w[k] for k in _ENABLED)
    if abs(_t - 1.0) > 1e-6:
        raise SystemExit(f"{_n} weights total {_t:.6f}, expected 1.0")

ARMS = {
    "baseline":            {"weights": None,     "margin": "model",  "prior": None},
    "no-srs":              {"weights": W_NO_SRS, "margin": "model",  "prior": None},
    "low-srs":             {"weights": W_LOW_SRS,"margin": "model",  "prior": None},
    "rest-days":           {"weights": None,     "margin": "model",  "prior": None, "rest": True},
    "market-anchor-open":  {"weights": None,     "margin": "market", "prior": "open"},
    "market-anchor-close": {"weights": None,     "margin": "market", "prior": "close"},
    "market+rest-open":    {"weights": None,     "margin": "market", "prior": "open",  "rest": True},
    "market+rest-close":   {"weights": None,     "margin": "market", "prior": "close", "rest": True},
    # needs a point-in-time SRS solver (see docs); not runnable until that exists
    "fresh-srs":           {"weights": None,     "margin": "model",  "prior": None, "srs_source": "computed"},
}

REST_GRID = [round(0.0 + 0.1 * i, 2) for i in range(16)]      # 0.0 .. 1.5 pts/day
SCALING_GRID = [round(0.0 + 0.05 * i, 2) for i in range(21)]   # 0.0 .. 1.0


# ---------------------------------------------------------------------------------------
# Phase A — build records
# ---------------------------------------------------------------------------------------
def _prev_dates(games: list[dict]) -> dict[str, list[str]]:
    per: dict[str, list[str]] = {}
    for g in games:
        d = g.get("startDate")
        if not d:
            continue
        for side in ("homeTeam", "awayTeam"):
            t = g.get(side)
            if t:
                per.setdefault(t, []).append(d)
    for t in per:
        per[t] = sorted(set(per[t]))
    return per


def _rest_days(per_team: dict[str, list[str]], team: str, when: str):
    """Days since this team's previous game, or None for a team's first game of the season."""
    dates = per_team.get(team)
    if not dates or not when:
        return None
    prev = None
    for d in dates:
        if d < when:
            prev = d
        else:
            break
    if prev is None:
        return None
    try:
        a = datetime.fromisoformat(prev.replace("Z", "+00:00"))
        b = datetime.fromisoformat(when.replace("Z", "+00:00"))
        return (b - a).total_seconds() / 86400.0
    except Exception:  # noqa: BLE001
        return None


def build_records(season: int, harness) -> list[dict]:
    """Per-game records for a season: PIT team data, book lines, rest, result."""
    import app
    import run_model_backtest as hb  # noqa: F811 (name kept for callers)

    prior = json.loads((CACHE / f"preseason_{season}.json").read_text(encoding="utf-8"))
    games = json.loads((CACHE / f"lines_{season}.json").read_text(encoding="utf-8"))
    completed_incl = [g for g in games
                      if g.get("homeScore") is not None and g.get("awayScore") is not None]
    weeks = sorted({int(g.get("week") or 0) for g in completed_incl})
    past_weeks = tuple(range(1, max(weeks))) if weeks else ()
    per_team = _prev_dates(games)

    # client is unused by the cached path but the signature wants one
    client = app.httpx.Client(timeout=20)
    weekly = {}
    for wk in past_weeks:
        fp = CACHE / f"stats_game_advanced_{season}_wk{wk}.json"
        if fp.exists():
            weekly[wk] = json.loads(fp.read_text(encoding="utf-8"))

    recs = []
    for g in completed_incl:
        wk = int(g.get("week") or 0)
        h_name, a_name = g.get("homeTeam"), g.get("awayTeam")
        when = g.get("startDate")
        home_pit = hb.reconstruct_pit_team_data(prior, weekly, h_name, wk)
        away_pit = hb.reconstruct_pit_team_data(prior, weekly, a_name, wk)
        if not home_pit or not away_pit:
            continue
        line = None
        for prov in PROVIDER_PREFERENCE:
            for l in g.get("lines") or []:
                if l.get("provider") == prov and l.get("spread") is not None:
                    line = l
                    break
            if line:
                break
        if not line:
            continue
        is_fbs = all(p.get("sp_plus") is not None and p.get("sp_plus") != 0
                     for p in (home_pit, away_pit))
        recs.append({
            "season": season, "week": wk, "game_id": g.get("id"),
            "home": h_name, "away": a_name, "provider": line.get("provider"),
            "spread_close": float(line["spread"]),
            "spread_open": (float(line["spreadOpen"])
                            if line.get("spreadOpen") is not None else None),
            "total_close": (float(line["overUnder"])
                            if line.get("overUnder") is not None else None),
            "home_score": g.get("homeScore"), "away_score": g.get("awayScore"),
            "is_fbs": bool(is_fbs),
            "rest_home": _rest_days(per_team, h_name, when),
            "rest_away": _rest_days(per_team, a_name, when),
            "home_pit": home_pit, "away_pit": away_pit,
        })
    return recs


def add_model_columns(recs: list[dict], weights: dict | None) -> None:
    """Attach the production projection (composite gap, model margin/total) to each record.

    The composite math is app.py's — this sets the weights via the documented override and
    calls the real projection, rather than reimplementing normalisation here.
    """
    import app
    prev = os.environ.get("COMPOSITE_WEIGHTS_JSON")
    if weights is None:
        os.environ.pop("COMPOSITE_WEIGHTS_JSON", None)
    else:
        os.environ["COMPOSITE_WEIGHTS_JSON"] = json.dumps(weights)
    try:
        for r in recs:
            h2h = app.project_head_to_head(r["home_pit"], r["away_pit"])
            r["edge"] = h2h["home_composite"] - h2h["away_composite"]
            r["model_margin"] = h2h["differential"]
            r["model_total"] = h2h["total"]
            r["model_version"] = app.composite_version()
    finally:
        if prev is None:
            os.environ.pop("COMPOSITE_WEIGHTS_JSON", None)
        else:
            os.environ["COMPOSITE_WEIGHTS_JSON"] = prev


# ---------------------------------------------------------------------------------------
# Phase B — formulas and metrics
# ---------------------------------------------------------------------------------------
def arm_margin(r: dict, arm: dict, scaling=0.0, rest_factor=0.0):
    """Return (model_margin, usable) for this arm's formula. usable=False -> skip the game."""
    if arm["margin"] == "market":
        key = "spread_open" if arm.get("prior") == "open" else "spread_close"
        raw = r.get(key)
        if raw is None:
            return None, False
        # CFBD spread is home-perspective with favourites negative; -spread = expected home
        # margin. The edge is added in the same (home) frame so signs cannot silently invert.
        m = (-raw) + scaling * r["edge"]
    else:
        m = r["model_margin"]
    if arm.get("rest"):
        rh, ra = r.get("rest_home"), r.get("rest_away")
        if rh is not None and ra is not None:
            m = m + (rh - ra) * rest_factor
    return m, True


def evaluate(recs: list[dict], arm: dict, scaling=0.0, rest_factor=0.0,
             seasons: list[int] | None = None) -> dict:
    use = [r for r in recs if seasons is None or r["season"] in seasons]
    m = {"n": 0, "fbs_n": 0, "nonfbs_n": 0,
         "ats_w": 0, "ats_l": 0, "ats_p": 0, "fbs_ats_w": 0, "fbs_ats_l": 0, "fbs_ats_p": 0,
         "nf_ats_w": 0, "nf_ats_l": 0, "nf_ats_p": 0,
         "ou_w": 0, "ou_l": 0, "ou_p": 0,
         "su_w": 0, "su_l": 0,
         "abs_model": 0.0, "abs_book": 0.0, "abs_err": 0.0, "bias": 0.0}
    for r in use:
        mm, ok = arm_margin(r, arm, scaling, rest_factor)
        if not ok:
            continue
        bm = -r["spread_close"]
        am = r["home_score"] - r["away_score"]
        m["n"] += 1
        m["fbs_n" if r["is_fbs"] else "nonfbs_n"] += 1
        cover = am - bm
        pick_home = mm > bm
        if cover == 0:
            m["ats_p"] += 1
            if r["is_fbs"]: m["fbs_ats_p"] += 1
            else: m["nf_ats_p"] += 1
        elif (pick_home and cover > 0) or (not pick_home and cover < 0):
            m["ats_w"] += 1
            if r["is_fbs"]: m["fbs_ats_w"] += 1
            else: m["nf_ats_w"] += 1
        else:
            m["ats_l"] += 1
            if r["is_fbs"]: m["fbs_ats_l"] += 1
            else: m["nf_ats_l"] += 1
        if r["total_close"] is not None:
            at = r["home_score"] + r["away_score"]
            if mm is not None and at == r["total_close"]:
                m["ou_p"] += 1
            elif (r["model_total"] > r["total_close"]) == (at > r["total_close"]):
                m["ou_w"] += 1
            else:
                m["ou_l"] += 1
        if am != 0 and mm != 0:
            if (mm > 0) == (am > 0):
                m["su_w"] += 1
            else:
                m["su_l"] += 1
        m["abs_model"] += abs(mm)
        m["abs_book"] += abs(bm)
        m["abs_err"] += abs(mm - bm)
        m["bias"] += (mm - bm)
    return m


def fmt(m: dict) -> dict:
    w, l, p = m["ats_w"], m["ats_l"], m["ats_p"]
    d = w + l
    fw, fl = m["fbs_ats_w"], m["fbs_ats_l"]
    fd = fw + fl
    nw, nl = m["nf_ats_w"], m["nf_ats_l"]
    nd = nw + nl
    ow, ol = m["ou_w"], m["ou_l"]
    od = ow + ol
    sw, sl = m["su_w"], m["su_l"]
    sd = sw + sl
    n = max(1, m["n"])
    return {
        "games": m["n"], "fbs_games": m["fbs_n"], "nonfbs_games": m["nonfbs_n"],
        "ats": f"{w}-{l}-{p}", "ats_pct": round(100 * w / d, 1) if d else None,
        "ats_fbs": f"{fw}-{fl}", "ats_fbs_pct": round(100 * fw / fd, 1) if fd else None,
        "ats_nonfbs": f"{nw}-{nl}", "ats_nonfbs_pct": round(100 * nw / nd, 1) if nd else None,
        "ou": f"{ow}-{ol}", "ou_pct": round(100 * ow / od, 1) if od else None,
        "su": f"{sw}-{sl}", "su_pct": round(100 * sw / sd, 1) if sd else None,
        "mean_model_margin": round(m["abs_model"] / n, 2),
        "mean_book_line": round(m["abs_book"] / n, 2),
        "mae_vs_book": round(m["abs_err"] / n, 2),
        "bias_vs_book": round(m["bias"] / n, 2),
    }


def objective(m: dict) -> float:
    """Fit objective: ATS win rate on FBS matchups (the market this study cares about)."""
    d = m["fbs_ats_w"] + m["fbs_ats_l"]
    return (m["fbs_ats_w"] / d) if d else 0.0


def main() -> int:
    ap = argparse.ArgumentParser(description="Run a set of backtest arms across seasons.")
    ap.add_argument("--seasons", type=int, nargs="+", required=True)
    ap.add_argument("--arms", default=None, help="comma-separated arm names (default: all runnable)")
    ap.add_argument("--fit", default=None, help="arm to fit parameters for (grid search)")
    ap.add_argument("--holdout", type=int, default=None, help="season held out during fitting")
    ap.add_argument("--out", default=None, help="JSON report destination")
    args = ap.parse_args()

    import run_model_backtest as hb  # noqa: F401  (module-level pieces reused above)
    OUT.mkdir(parents=True, exist_ok=True)

    print(f"[study] building records for {args.seasons}")
    recs: list[dict] = []
    for s in args.seasons:
        rs = build_records(s, hb)
        recs.extend(rs)
        print(f"[study]   {s}: {len(rs)} games ({sum(1 for r in rs if r['is_fbs'])} FBS)")

    # One model pass per distinct weights config.
    configs: dict[str, dict | None] = {}
    for name, arm in ARMS.items():
        configs.setdefault(json.dumps(arm.get("weights"), sort_keys=True), arm.get("weights"))

    print(f"[study] model passes: {len(configs)} weight config(s)")
    tagged: list[dict] = []
    for key, weights in configs.items():
        block = json.loads(json.dumps(recs))  # deep copy per config
        add_model_columns(block, weights)
        for r in block:
            r["_wkey"] = key
        tagged.extend(block)
        import app
        print(f"[study]   weights={'defaults' if weights is None else 'custom':<9} "
              f"model_version={block[0]['model_version'] if block else '?'}")

    # Arm evaluation: pick the record block matching the arm's weights.
    def block_for(arm):
        k = json.dumps(arm.get("weights"), sort_keys=True)
        return [r for r in tagged if r["_wkey"] == k]

    names = ([a.strip() for a in args.arms.split(",")] if args.arms
             else [n for n, a in ARMS.items() if a.get("srs_source") != "computed"])
    report: dict = {"seasons": args.seasons, "arms": {}}
    for name in names:
        arm = ARMS[name]
        recs_a = block_for(arm)
        scaling, rest_f = 0.0, 0.0
        fit_info = None
        # Any arm with a free parameter is FITTED HERE, once per run — fitting on all seasons
        # except the holdout, then reporting the holdout separately. Fitting and scoring on the
        # same seasons would make the headline an in-sample artifact.
        if arm["margin"] == "market" or arm.get("rest"):
            fit_seasons = [s for s in args.seasons if s != args.holdout]
            grid_s = SCALING_GRID if arm["margin"] == "market" else [0.0]
            grid_r = REST_GRID if arm.get("rest") else [0.0]
            best = (-1.0, 0.0, 0.0)
            for sc in grid_s:
                for rf in grid_r:
                    v = objective(evaluate(recs_a, arm, sc, rf, seasons=fit_seasons))
                    if v > best[0]:
                        best = (v, sc, rf)
            scaling, rest_f = best[1], best[2]
            oos = evaluate(recs_a, arm, scaling, rest_f,
                           seasons=[args.holdout] if args.holdout else args.seasons)
            fit_info = {"fit_on": fit_seasons, "holdout": args.holdout,
                        "in_sample_ats_pct": round(100 * best[0], 1),
                        "scaling_factor": scaling, "rest_factor": rest_f,
                        "holdout_ats": fmt(oos)["ats_fbs"],
                        "holdout_ats_pct": fmt(oos)["ats_fbs_pct"],
                        "holdout_games": fmt(oos)["fbs_games"]}
        pooled = evaluate(recs_a, arm, scaling, rest_f)
        per_season = {str(s): fmt(evaluate(recs_a, arm, scaling, rest_f, seasons=[s]))
                      for s in args.seasons}
        report["arms"][name] = {
            "pooled": fmt(pooled),
            "per_season": per_season,
            "scaling_factor": scaling if arm["margin"] == "market" else None,
            "rest_factor": rest_f if arm.get("rest") else None,
            "fit": fit_info,
        }
        p = report["arms"][name]["pooled"]
        extra = ""
        if arm["margin"] == "market":
            extra += f" scaling={scaling}"
        if arm.get("rest"):
            extra += f" rest_factor={rest_f}"
        print(f"[study] {name:<22} ATS-FBS {p['ats_fbs_pct']}% ({p['ats_fbs']})  "
              f"O/U {p['ou_pct']}%  SU {p['su_pct']}%  MAE {p['mae_vs_book']}{extra}")

    dest = Path(args.out) if args.out else OUT / f"study_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    dest.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"[study] report -> {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())