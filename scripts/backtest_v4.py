#!/usr/bin/env python3
"""backtest_v4.py — V4 PHASE 2: the three arms that Phase 1's harvest made possible.

Handoff section 4 (`docs/SESSION_HANDOFF.md`) defines Phase 2 as:

  ARM 1  combined: nonlinear power 1.7  +  REAL line movement (close-open) x move_factor,
         slope refit under the combination (V3 tested the two separately and never together).
  ARM 2  market-anchor frame on **BetOnline.ag's opening line** (NOT Pinnacle), composite_edge x k.
         The V3/V2 market arms anchored on the CFBD/consensus line — the price Jeff often cannot bet.
  ARM 3  totals model with scales **FITTED on 2021-24 by MAE** (never guessed).

STANDING FITTING RULE (Phase 1 lesson, do not violate):
    fit on MAE, never ATS alone. An ATS-only objective rewards shrinking the model; on 2021-24 the
    ATS-maximising slope collapsed to 0.014 with MAE 11.66 — a degenerate model that still scores.
    Calibration cannot be gamed by shrinking, so slope/scale parameters are fit by MAE and the ATS
    number is reported, never optimised directly.

LINE SOURCE
    Every arm is reported against the SAME grading line (the CFBD close already used by V2/V3) so the
    numbers stay comparable, plus a BetOnline-graded variant wherever his book has both ends, because
    that is the price he can actually take.

Usage:
    python scripts/backtest_v4.py --coverage                       # join probe only, no fitting
    python scripts/backtest_v4.py --seasons 2021 2022 2023 2024 2025 --holdout 2025
    python scripts/backtest_v4.py --arms 1,2 --seasons ...         # subset
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

# Importing app runs _start_scheduler() (app.py:5163), which fires a LIVE refresh_all()
# ~10s later — so a backtest silently spent Odds API / PropLine credits and rewrote live
# caches. REFRESH_INTERVAL_SECONDS<=0 makes the scheduler return immediately, which is the
# supported switch for it. Set BEFORE app is imported.
os.environ.setdefault("REFRESH_INTERVAL_SECONDS", "0")

from backtest_study import build_records, evaluate, fmt  # noqa: E402

CACHE = ROOT / "data" / "backtest_cache"
HIST = ROOT / "data" / "odds_history"
OUT = ROOT / "data" / "backtest_study"

BOOK = "betonlineag"

# Kickoff agreement window for the odds-history join. Snapshots are weekly, so one game's
# nearest observation can be days away — but a MISMATCHED game is a different game entirely.
# 36h covers a reschedule without ever crossing into another week's slate.
KICKOFF_TOL_H = 36.0

# Our book's close vs the CFBD close may differ by a point or two (different book, different
# snapshot) — but a >7-pt gap is not a pricing difference, it is the WRONG GAME's line.
DIVERGENCE_PTS = 7.0

POWER_GRID = [round(0.8 + 0.1 * i, 2) for i in range(14)]      # 0.8 .. 2.1
SLOPE_GRID = [round(0.01 * (1.13 ** i), 4) for i in range(48)]  # 0.01 .. ~3.0 log-spaced
MOVE_GRID = [round(-0.5 + 0.1 * i, 2) for i in range(21)]       # -0.5 .. 1.5 (sign is a finding, not an assumption)
K_GRID = [round(0.0025 * i, 4) for i in range(121)]             # 0.0 .. 0.30

# ARM 3 grids — deliberately coarse then refined around the optimum, and the optimum is FLAGGED
# if it lands on a grid edge (the V3 lesson: a grid-edge optimum is not an optimum).
T3_B0 = [round(10 + 2.0 * i, 1) for i in range(21)]     # 10 .. 50
T3_B1 = [round(0.0 + 0.05 * i, 2) for i in range(31)]   # 0.0 .. 1.5  (offense scale)
T3_B2 = [round(0.0 + 0.05 * i, 2) for i in range(31)]   # 0.0 .. 1.5  (defense scale)


# ---------------------------------------------------------------------------------------
# Phase 1 harvest -> per-game open/close lines for one book
# ---------------------------------------------------------------------------------------
def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_book_lines(book: str = BOOK) -> dict:
    """game key -> {open, close, total_open, total_close, commence} using the Phase 1 rule.

    Open  = earliest pre-kickoff observation of that book's line.
    Close = latest   pre-kickoff observation.
    (Snapshots are weekly; a game simply is not posted at the "open" timestamp, which is why the
    naive "present in the open file AND the close file" test understates coverage 10x.)
    """
    games = {}
    for path in sorted(glob.glob(str(HIST / "*.json"))):
        week = json.loads(Path(path).read_text(encoding="utf-8"))
        for snap in week.get("snapshots", []):
            ts = _dt(snap["snapshot_ts"])
            for ev in snap.get("events", []):
                g = games.setdefault(ev["id"], {
                    "commence": ev["commence_time"], "home": ev["home_team"],
                    "away": ev["away_team"], "obs": []})
                b = (ev.get("bookmakers") or {}).get(book) or {}
                # BOTH outcomes are kept with their team names. The feed does NOT guarantee
                # home-first ordering (observed: [{away +9.5}, {home -9.5}]), so taking
                # spreads[0] silently flips the sign for those games. Resolve by name later.
                spr = [(o.get("name"), o.get("point")) for o in b.get("spreads") or []
                       if o.get("point") is not None]
                tot = next((o.get("point") for o in b.get("totals") or []
                            if o.get("point") is not None), None)
                g["obs"].append((ts, spr, tot))
    out = {}
    for gid, g in games.items():
        ct = _dt(g["commence"])
        pre = sorted([o for o in g["obs"] if o[0] < ct], key=lambda x: x[0])
        if not pre:
            continue
        first, last = pre[0], pre[-1]
        out[gid] = {"commence": g["commence"], "home": g["home"], "away": g["away"],
                    "open": first[1], "close": last[1],
                    "total_open": first[2], "total_close": last[2],
                    "n_obs": len(pre)}
    return out


def _cfbd_name_index(seasons: list[int]) -> dict:
    """normalized-name -> CFBD team name, straight from the cached line files (no guessing)."""
    idx = {}
    for s in seasons:
        for g in json.loads((CACHE / f"lines_{s}.json").read_text(encoding="utf-8")):
            for side in ("homeTeam", "awayTeam"):
                n = g.get(side)
                if n:
                    idx.setdefault(n.strip().lower(), n)
    return idx


def _strip_mascot(name: str, names: dict) -> str | None:
    """'James Madison Dukes' -> 'James Madison' by longest-prefix match on real CFBD team names."""
    low = (name or "").strip().lower()
    if low in names:
        return names[low]
    best = None
    for k, v in names.items():
        if low.startswith(k + " ") and (best is None or len(k) > len(best[0])):
            best = (k, v)
    return best[1] if best else None


def _home_spread(outcomes, names: dict, home: str, away: str):
    """Home-perspective spread (favourite negative) from the feed's (name, point) pairs.

    Resolves by TEAM NAME, never by list order: the feed interleaves home/away ordering, so
    positional access silently negates the line for those games.
    """
    home_pt = away_pt = None
    for nm, pt in outcomes or []:
        t = _strip_mascot(nm or "", names)
        if t is None:
            continue
        if t == home and home_pt is None:
            home_pt = pt
        elif t == away and away_pt is None:
            away_pt = pt
    if home_pt is not None:
        return home_pt
    if away_pt is not None:
        return -away_pt          # away-quoted -> home-relative
    return None


def join_lines(recs: list[dict], seasons: list[int], book: str = BOOK, prefix: str = "bo") -> dict:
    """Attach this book's open/close lines to the CFBD records. Returns a join report.

    MATCHING RULES (learned the hard way — a team-pair-only join silently attached OTHER
    GAMES' lines: measured |his close - CFBD close| p90 was 40 pts with a 109-pt maximum,
    which made the model appear to score 64.6% ATS against a line that was not its game's):
      1. season must agree (derived from the odds event's kickoff, Aug-Dec = that year,
         Jan = the previous season),
      2. both team names must resolve to the same CFBD teams,
      3. kickoff must agree within KICKOFF_TOL_H hours,
      4. ties (same pairing twice) resolve to the nearest kickoff.
    """
    names = _cfbd_name_index(seasons)
    book_games = load_book_lines(book)
    by_pair: dict[tuple, list] = {}
    for gid, g in book_games.items():
        h = _strip_mascot(g["home"], names)
        a = _strip_mascot(g["away"], names)
        if h and a:
            ct = _dt(g["commence"])
            szn = ct.year if ct.month >= 8 else ct.year - 1
            by_pair.setdefault((szn, h, a), []).append((gid, g))

    matched = 0
    unmatched_kick = 0
    diverged = 0
    for r in recs:
        cands = by_pair.get((r["season"], r["home"], r["away"]), [])
        hit = None
        if cands:
            hit = min(cands, key=lambda c: abs(
                (_dt(c[1]["commence"]) - _dt(r["start_iso"])).total_seconds()))
            delta_h = abs((_dt(hit[1]["commence"]) - _dt(r["start_iso"])).total_seconds()) / 3600.0
            if delta_h > KICKOFF_TOL_H:
                unmatched_kick += 1
                hit = None
        ho = _home_spread(hit[1]["open"], names, r["home"], r["away"]) if hit else None
        hc = _home_spread(hit[1]["close"], names, r["home"], r["away"]) if hit else None
        r[f"{prefix}_open"], r[f"{prefix}_close"] = ho, hc
        r[f"{prefix}_total_open"] = hit[1]["total_open"] if hit else None
        r[f"{prefix}_total_close"] = hit[1]["total_close"] if hit else None
        r[f"{prefix}_obs"] = hit[1]["n_obs"] if hit else 0
        if hit:
            matched += 1
        # SANITY: our book's close vs the CFBD close must be the SAME GAME's line. A large
        # gap means the join attached the wrong game (or the sign flipped) — count it loudly
        # rather than letting it silently manufacture a fake edge.
        if hc is not None and r.get("spread_close") is not None:
            if abs(hc - r["spread_close"]) > DIVERGENCE_PTS:
                diverged += 1
    n_bo_open = sum(1 for r in recs if r[f"{prefix}_open"] is not None)
    n_bo_both = sum(1 for r in recs if r[f"{prefix}_open"] is not None and r[f"{prefix}_close"] is not None)
    n_bo_tot = sum(1 for r in recs if r[f"{prefix}_total_close"] is not None)
    return {"book": book, "recs": len(recs), "matched_pair": matched,
            "rejected_kickoff_mismatch": unmatched_kick,
            "close_diverges_from_cfbd": diverged,
            "with_bo_open": n_bo_open, "with_bo_open_and_close": n_bo_both,
            "with_bo_total_close": n_bo_tot}


# ---------------------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------------------
def load(seasons: list[int]) -> list[dict]:
    import app
    recs = []
    for s in seasons:
        rs = build_records(s, None)
        for r in rs:
            r["start_iso"] = None
        games = {g.get("id"): g for g in json.loads((CACHE / f"lines_{s}.json").read_text(encoding="utf-8"))}
        for r in rs:
            g = games.get(r["game_id"]) or {}
            r["start_iso"] = g.get("startDate")
            h2h = app.project_head_to_head(r["home_pit"], r["away_pit"])
            r["edge"] = h2h["home_composite"] - h2h["away_composite"]
            r["prod_margin"] = h2h["differential"]
            r["prod_total"] = h2h["total"]
            r["model_version"] = app.composite_version()
            # ARM 1 input: real movement in the home-margin frame (CFBD perspective, favourites negative)
            so, sc = r.get("spread_open"), r.get("spread_close")
            r["move"] = (sc - so) if (so is not None and sc is not None) else None
        recs.extend(rs)
    return recs


def finalize(recs: list[dict]) -> None:
    """His-book prior chain, computed AFTER the odds-history join.

    PRIOR CHAIN (Jeff, 2026-09-24 — "use William Hill for everything betonlineag misses"):
    BetOnline.ag first; William Hill Nevada fills everything it misses. Both are books he can
    actually bet, so the market arm always anchors on a bettable price.
    """
    for r in recs:
        if r.get("bo_open") is not None:
            src = "betonlineag"
            o, c = r.get("bo_open"), r.get("bo_close")
        elif r.get("wh_open") is not None:
            src = "williamhill_us"
            o, c = r.get("wh_open"), r.get("wh_close")
        else:
            src, o, c = None, None, None
        r["prior_src"], r["prior_open"], r["prior_close"] = src, o, c
        r["move_book"] = (c - o) if (o is not None and c is not None) else None
        r["prior_total"] = (r.get("bo_total_close") if r.get("bo_total_close") is not None
                            else r.get("wh_total_close"))


# ---------------------------------------------------------------------------------------
# formulas
# ---------------------------------------------------------------------------------------
def mae_of(recs, fit_seasons):
    m = evaluate([r for r in recs if r["season"] in fit_seasons], {"margin": "model"})
    return m["abs_err"] / max(1, m["n"])


def set_margin(recs, fn):
    for r in recs:
        r["model_margin"] = fn(r)


def arm1_margin(r, power, slope, move_f):
    d = r["edge"]
    nl = math.copysign(abs(d) ** power, d) * slope
    m = r.get("move")
    extra = (m * move_f) if m is not None else 0.0
    return nl + extra


def arm1_margin_book(r, power, slope, move_f):
    """ARM 1 with the movement term measured on HIS books, not the CFBD line."""
    d = r["edge"]
    nl = math.copysign(abs(d) ** power, d) * slope
    m = r.get("move_book")
    extra = (m * move_f) if m is not None else 0.0
    return nl + extra


def arm2_margin(r, k):
    """market anchor: his book's opening line is the prior; the composite only nudges it.

    PRIOR CHAIN (Jeff, 2026-09-24 — "use William Hill for everything betonlineag misses"):
    BetOnline.ag opener, then William Hill Nevada (feed key `williamhill_us`) when the opener
    is not on the board. Both are books he can actually bet, so the anchor stays bettable;
    only games where NEITHER has posted fall out of the arm.
    """
    prior = r.get("prior_open")
    return (-prior) + k * r["edge"]


def arm3_total(r, b0, b1, b2):
    ho, ao = r["home_pit"].get("sp_offense"), r["away_pit"].get("sp_offense")
    hd, ad = r["home_pit"].get("sp_defense"), r["away_pit"].get("sp_defense")
    if None in (ho, ao, hd, ad):
        return None
    return b0 + b1 * ((ho + ao) / 2.0) + b2 * ((hd + ad) / 2.0)


# ---------------------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="V4 Phase 2 arms")
    ap.add_argument("--seasons", type=int, nargs="+", default=[2021, 2022, 2023, 2024, 2025])
    ap.add_argument("--holdout", type=int, default=2025)
    ap.add_argument("--arms", default="1,2,3")
    ap.add_argument("--coverage", action="store_true", help="join probe only (no fitting)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    arms = {a.strip() for a in args.arms.split(",")}
    fit_s = [s for s in args.seasons if s != args.holdout]

    print(f"[v4] loading records for {args.seasons}")
    recs = load(args.seasons)
    jr = join_lines(recs, args.seasons, "betonlineag", prefix="bo")
    jr_wh = join_lines(recs, args.seasons, "williamhill_us", prefix="wh")
    finalize(recs)
    print(f"[v4] book join betonlineag: {jr}")
    print(f"[v4] book join williamhill_us: {jr_wh}")
    n_prior = sum(1 for r in recs if r["prior_open"] is not None)
    n_from_wh = sum(1 for r in recs if r["prior_src"] == "williamhill_us")
    print(f"[v4] prior chain (betonlineag -> williamhill_us): {n_prior}/{len(recs)} games, "
          f"of which {n_from_wh} come from William Hill")
    print(f"[v4] records {len(recs)} across {len(args.seasons)} seasons "
          f"(model_version {recs[0]['model_version'] if recs else '?'})")

    if args.coverage:
        per = {}
        for s in args.seasons:
            sub = [r for r in recs if r["season"] == s]
            per[str(s)] = {
                "games": len(sub),
                "bo_open": sum(1 for r in sub if r["bo_open"] is not None),
                "wh_open": sum(1 for r in sub if r["wh_open"] is not None),
                "prior_open": sum(1 for r in sub if r["prior_open"] is not None),
                "prior_from_wh": sum(1 for r in sub if r["prior_src"] == "williamhill_us"),
                "prior_open_and_close": sum(1 for r in sub if r["prior_open"] is not None and r["prior_close"] is not None),
                "prior_total": sum(1 for r in sub if r["prior_total"] is not None),
                "move_cfbd": sum(1 for r in sub if r.get("move") is not None),
            }
        print(json.dumps(per, indent=2))
        return 0

    report = {"seasons": args.seasons, "holdout": args.holdout, "book": BOOK,
              "join": jr, "model_version": recs[0]["model_version"], "arms": {}}

    def show(name, recs_x, extra=""):
        pooled = fmt(evaluate(recs_x, {"margin": "model"}))
        hold = fmt(evaluate([r for r in recs_x if r["season"] == args.holdout], {"margin": "model"}))
        per = {str(s): fmt(evaluate([r for r in recs_x if r["season"] == s], {"margin": "model"}))
               for s in args.seasons}
        vals = [v["ats_fbs_pct"] for v in per.values() if v["ats_fbs_pct"] is not None]
        band = round(max(vals) - min(vals), 2) if vals else None
        report["arms"][name] = {"pooled": pooled, "per_season": per, "band": band, "note": extra}
        print(f"[v4] {name:<22} ATS {pooled['ats_fbs_pct']}% ({pooled['ats_fbs']}, {pooled['fbs_games']})  "
              f"O/U {pooled['ou_pct']}%  SU {pooled['su_pct']}%  MAE {pooled['mae_vs_book']}  "
              f"band {band}pp  held-out {hold['ats_fbs_pct']}%")
        return report["arms"][name]

    # reference: production formula at live weights
    set_margin(recs, lambda r: r["prod_margin"])
    for r in recs:
        r["model_total"] = r["prod_total"]
    show("ref-production", recs, "production projection, live weights")

    # ---------------- ARM 1: power + real line movement, slope refit ----------------
    if "1" in arms:
        best = (1e9, None, None, None)
        grid = []
        for pw in POWER_GRID:
            for mf in MOVE_GRID:
                bs = (1e9, None)
                for sl in SLOPE_GRID:
                    set_margin(recs, lambda r, pw=pw, sl=sl, mf=mf: arm1_margin(r, pw, sl, mf))
                    m = mae_of(recs, fit_s)
                    if m < bs[0]:
                        bs = (m, sl)
                grid.append({"power": pw, "move_factor": mf, "slope": bs[1], "mae_fit": round(bs[0], 3)})
                if bs[0] < best[0]:
                    best = (bs[0], pw, bs[1], mf)
        _, pw, sl, mf = best
        edge_pw = pw in (POWER_GRID[0], POWER_GRID[-1])
        edge_mf = mf in (MOVE_GRID[0], MOVE_GRID[-1])
        print(f"[v4] arm1 best: power {pw} slope {sl} move_factor {mf} (fit MAE {best[0]:.3f})"
              f"{'  [GRID-EDGE power]' if edge_pw else ''}{'  [GRID-EDGE move]' if edge_mf else ''}")
        set_margin(recs, lambda r, pw=pw, sl=sl, mf=mf: arm1_margin(r, pw, sl, mf))
        for r in recs:
            r["model_total"] = r["prod_total"]
        a1 = show("arm1-power+move", recs, f"power={pw} slope={sl} move_factor={mf}")
        a1["params"] = {"power": pw, "slope": sl, "move_factor": mf, "fit_mae": round(best[0], 3),
                        "grid_edge_power": edge_pw, "grid_edge_move": edge_mf}
        a1["grid"] = grid
        # the same, restricted to games that actually HAVE movement (the honest read)
        sub = [r for r in recs if r.get("move") is not None and r["move"] != 0.0]
        if sub:
            show("arm1-move-present", sub, "subset: games with non-zero CFBD movement")
        # same shape, but movement measured on HIS books (Phase 1's actual purpose)
        sub2 = [r for r in recs if r.get("move_book") is not None and r["move_book"] != 0.0]
        if sub2:
            b2 = (1e9, None)
            for mf2 in MOVE_GRID:
                for sl2 in SLOPE_GRID:
                    set_margin(sub2, lambda r, pw=pw, sl=sl2, mf=mf2: arm1_margin_book(r, pw, sl, mf2))
                    m2 = mae_of(sub2, fit_s)
                    if m2 < b2[0]:
                        b2 = (m2, mf2)
            for r in sub2:
                r["model_total"] = r["prod_total"]
            set_margin(sub2, lambda r, pw=pw, mf=b2[1]: arm1_margin_book(r, pw, sl, mf))
            show("arm1-hisbook-move", sub2, f"power={pw} slope={sl} move_factor={b2[1]} (his-book movement)")

    # ---------------- ARM 2: market anchor on HIS opening line (BetOnline.ag -> William Hill) ----------------
    if "2" in arms:
        sub = [r for r in recs if r["prior_open"] is not None]
        src_split = {k: sum(1 for r in sub if r["prior_src"] == k)
                     for k in ("betonlineag", "williamhill_us")}
        print(f"[v4] arm2 coverage: {len(sub)} games with a bettable opener "
              f"(betonlineag {src_split['betonlineag']}, williamhill_us {src_split['williamhill_us']})")
        # COVERAGE NOTE (probed, not assumed): The Odds API's archive has NO betonlineag
        # bookmakers before 2022. Jeff's rule ("use William Hill for everything betonlineag
        # misses") is what restores 2021 to the arm at all.
        cov = {s: sum(1 for r in sub if r["season"] == s) for s in args.seasons}
        print(f"[v4] arm2 per-season opener coverage: {cov}")
        fit2 = [s for s in fit_s if cov.get(s, 0) >= 100] or fit_s
        if fit2 != fit_s:
            print(f"[v4] arm2 fit seasons restricted to {fit2} (insufficient book coverage in "
                  f"{[s for s in fit_s if s not in fit2]})")
        if not sub:
            print("[v4] arm2 skipped — no joinable openers from his books")
        else:
            bestk = (1e9, None)
            for k in K_GRID:
                set_margin(sub, lambda r, k=k: arm2_margin(r, k))
                m = mae_of(sub, fit2)
                if m < bestk[0]:
                    bestk = (m, k)
            k = bestk[1]
            set_margin(sub, lambda r, k=k: arm2_margin(r, k))
            for r in sub:
                r["model_total"] = r["prod_total"]
            edge_k = k in (K_GRID[0], K_GRID[-1])
            print(f"[v4] arm2 best: k {k} (fit MAE {bestk[0]:.3f})"
                  f"{'  [GRID-EDGE k]' if edge_k else ''}")
            a2 = show("arm2-hisbook-open-anchor", sub, f"prior=betonlineag->williamhill_us open, k={k}")
            a2["params"] = {"k": k, "fit_mae": round(bestk[0], 3), "grid_edge_k": edge_k,
                            "coverage": len(sub), "prior_source_split": src_split,
                            "prior_chain": "betonlineag -> williamhill_us"}
            # k fitted by ATS instead — reported to show the degenerate objective, not to ship
            besta = (-1.0, None)
            for k2 in K_GRID:
                set_margin(sub, lambda r, k2=k2: arm2_margin(r, k2))
                v = evaluate([r for r in sub if r["season"] in fit2], {"margin": "model"})
                d = v["fbs_ats_w"] + v["fbs_ats_l"]
                if d and (v["fbs_ats_w"] / d) > besta[0]:
                    besta = (v["fbs_ats_w"] / d, k2)
            a2["ats_fitted_k_diagnostic"] = {"k": besta[1], "ats_pct": round(100 * besta[0], 1),
                                             "note": "shown to demonstrate the degenerate objective"}
            # same picks, graded against HIS close (both ends required)
            graded = [dict(r) for r in sub if r["prior_close"] is not None]
            for r in graded:
                r["spread_close"] = r["prior_close"]
            set_margin(graded, lambda r, k=k: arm2_margin(r, k))
            show("arm2-graded-his-close", graded, "same picks, graded vs his own close")
            # betonlineag-only subset: does the William Hill fallback help or hurt?
            bo_only = [r for r in graded if r["prior_src"] == "betonlineag"]
            if bo_only:
                set_margin(bo_only, lambda r, k=k: arm2_margin(r, k))
                show("arm2-betonlineag-only", bo_only, "subset: betonlineag openers only")
            wh_only = [r for r in graded if r["prior_src"] == "williamhill_us"]
            if wh_only:
                set_margin(wh_only, lambda r, k=k: arm2_margin(r, k))
                show("arm2-williamhill-only", wh_only, "subset: games BetOnline.ag misses")

    # ---------------- ARM 3: totals, scales FITTED by MAE ----------------
    if "3" in arms:
        tot = [r for r in recs if r["total_close"] is not None]
        have = [r for r in tot if arm3_total(r, 0, 0, 0) is not None]
        print(f"[v4] arm3 coverage: {len(have)} games with totals + SP+ off/def inputs")
        if not have:
            print("[v4] arm3 skipped — no coverage")
        else:
            best = (1e9, None, None, None)
            for b1 in T3_B1:
                for b2 in T3_B2:
                    for b0 in T3_B0:
                        for r in have:
                            r["model_total"] = arm3_total(r, b0, b1, b2)
                        m = evaluate([r for r in have if r["season"] in fit_s], {"margin": "model"})
                        ou = m["ou_w"] + m["ou_l"]
                        mae = sum(abs(r["model_total"] - r["total_close"]) for r in have
                                  if r["season"] in fit_s) / max(1, sum(1 for r in have if r["season"] in fit_s))
                        if mae < best[0]:
                            best = (mae, b0, b1, b2)
            _, b0, b1, b2 = best
            print(f"[v4] arm3 best: b0 {b0} b1 {b1} b2 {b2} (fit MAE {best[0]:.3f})")
            for r in have:
                r["model_total"] = arm3_total(r, b0, b1, b2)
                r["model_margin"] = r["prod_margin"]
            a3 = show("arm3-totals-fit", have, f"total = b0 + b1*avg(sp_off) + b2*avg(sp_def)")
            a3["params"] = {"b0": b0, "b1": b1, "b2": b2, "fit_mae": round(best[0], 3),
                            "b1_at_edge": b1 in (T3_B1[0], T3_B1[-1]),
                            "b2_at_edge": b2 in (T3_B2[0], T3_B2[-1]),
                            "b0_at_edge": b0 in (T3_B0[0], T3_B0[-1])}
            for r in have:
                r["model_total"] = r["prod_total"]
            show("arm3-ref-production", have, "production totals on the same subset")

    dest = Path(args.out) if args.out else OUT / f"v4_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    dest.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"[v4] report -> {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())