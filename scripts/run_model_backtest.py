"""
run_model_backtest.py
Strict point-in-time walk-forward backtest harness for cfb-power-rankings.
Evaluates model projections against verified closing spreads and totals from CFBD (/lines).

Enforces:
  1. Closing-line timestamp & provider provenance:
     - Captures closing_timestamp (ISO 8601 UTC kickoff freeze time) on every game row.
     - Explicit provider hierarchy: DraftKings (primary) -> Bovada (secondary).
     - Records closing_spread, spread_open, closing_total, total_open, and line_source.
  2. True Point-in-Time Data Reconstruction (Zero Future Leakage):
     - Loads frozen pre-season snapshot (data/cfbd_analytics_preseason.json) for baseline priors.
     - Fetches and aggregates weekly game-level efficiency stats strictly from prior weeks (past_wk < week).
     - Week 1 uses strictly pre-season priors (0 in-season stats existed before kickoff).
     - Weeks 2-4 only ingest stats from completed prior games, with zero future-week data leakage.
  3. FCS / Unrated Veto:
     - Non-FBS programs with sp_plus == 0 are strictly excluded from star betting tiers.

Outputs:
  data/backtest_fixture_2026.json  (game-level audit log with closing line timestamps)
  data/backtest_summary.json       (verified ATS win rates by tier and walk-forward week)
"""

import json
import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
import app

DATA_DIR = ROOT_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
FIXTURE_FILE = DATA_DIR / "backtest_fixture_2026.json"
SUMMARY_FILE = DATA_DIR / "backtest_summary.json"
PRESEASON_FILE = DATA_DIR / "cfbd_analytics_preseason.json"

PROVIDER_PREFERENCE = ("DraftKings", "Draft Kings", "Bovada")

CACHE_DIR = DATA_DIR / "backtest_cache"
REFRESH = False   # set by --refresh on the CLI


def _cached_json(name: str, fetch_fn):
    """Fetch once, read many: the point-in-time inputs are frozen per (endpoint, year, week).

    Two reasons this exists, and the first is the important one:

      1. INTEGRITY — every arm of an experiment must be evaluated against the SAME bytes.
         Re-fetching between arms would let a CFBD update land mid-experiment and
         silently invalidate the comparison, and the difference between arms would no
         longer be only the weights.
      2. SPEED — a cached run makes no network calls, so experiments are fast and can be
         re-run offline.

    Pass --refresh to deliberately re-fetch.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{name}.json"
    if path.exists() and not REFRESH:
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:  # noqa: BLE001
            print(f"[Backtest] cache unreadable ({name}: {e}); re-fetching")
    data = fetch_fn()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        print(f"[Backtest] cached {name} -> {path.name}")
    except Exception as e:  # noqa: BLE001
        print(f"[Backtest] cache write failed for {name}: {e}")
    return data


def build_weekly_stats_cache(client, weeks=(1, 2, 3)) -> dict:
    """Pre-fetch weekly game-level advanced stats for past completed weeks (cached)."""
    weekly_stats = {}
    for wk in weeks:
        def _fetch(wk=wk):
            url = f"{app.CFBD_BASE}/stats/game/advanced?year=2026&week={wk}"
            try:
                resp = client.get(url, headers=app.CFBD_HEADERS)
                if resp.status_code == 200:
                    return resp.json()
                print(f"[Warning] week {wk} stats: HTTP {resp.status_code}")
            except Exception as e:  # noqa: BLE001
                print(f"[Warning] Failed to fetch week {wk} game stats: {e}")
            return []
        weekly_stats[wk] = _cached_json(f"stats_game_advanced_2026_wk{wk}", _fetch)
    return weekly_stats

def reconstruct_pit_team_data(preseason_map: dict, weekly_stats: dict, team_name: str, week: int) -> dict:
    """
    Reconstructs team data strictly as it existed prior to kickoff of `week`.
    Week 1: 100% frozen preseason ratings (no in-season stats).
    Week > 1: Ingests only completed game stats from prior weeks (past_wk < week).
    """
    base = dict(preseason_map.get(team_name, {}))
    if not base:
        return {}
        
    if week == 1:
        base["off_success_rate"] = None
        base["def_success_rate"] = None
        base["epa_play"] = 0.0
        base["def_epa_play"] = 0.0
        return base

    off_sr_list = []
    def_sr_list = []
    epa_list = []
    def_epa_list = []

    for past_wk in range(1, week):
        for entry in weekly_stats.get(past_wk, []):
            if entry.get("team") == team_name:
                off = entry.get("offense", {})
                df = entry.get("defense", {})
                if off.get("successRate") is not None:
                    off_sr_list.append(off["successRate"])
                if df.get("successRate") is not None:
                    def_sr_list.append(df["successRate"])
                if off.get("ppa") is not None:
                    epa_list.append(off["ppa"])
                if df.get("ppa") is not None:
                    def_epa_list.append(df["ppa"])

    if off_sr_list:
        base["off_success_rate"] = sum(off_sr_list) / len(off_sr_list)
    if def_sr_list:
        base["def_success_rate"] = sum(def_sr_list) / len(def_sr_list)
    if epa_list:
        base["epa_play"] = sum(epa_list) / len(epa_list)
    if def_epa_list:
        base["def_epa_play"] = sum(def_epa_list) / len(def_epa_list)

    return base

def run_backtest(year: int = 2026, summary_out=None, fixture_out=None) -> dict:
    client = app.httpx.Client(timeout=20)
    
    # 1. Load frozen pre-season baseline (including frozen 247 Team Talent Composite)
    if not PRESEASON_FILE.exists():
        raise RuntimeError("Missing data/cfbd_analytics_preseason.json")
    with open(PRESEASON_FILE, "r", encoding="utf-8") as f:
        preseason_list = json.load(f)
    preseason_map = {t["name"]: t for t in preseason_list}

    # 2. Load CFBD closing lines and game scores (cached — see _cached_json)
    print(f"[Backtest] Loading CFBD closing lines and game scores for {year}...")
    url = f"{app.CFBD_BASE}/lines?year={year}"

    def _fetch_lines():
        resp = client.get(url, headers=app.CFBD_HEADERS)
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to fetch CFBD lines: HTTP {resp.status_code}")
        return resp.json()

    games_raw = _cached_json(f"lines_{year}", _fetch_lines)
    if not games_raw:
        # An empty payload would otherwise produce a confident-looking summary over zero
        # games. Refuse instead — a silent 0-game "result" is worse than an error.
        raise RuntimeError("no games loaded (empty lines payload/cache) — refusing to "
                           "report a backtest over zero games")

    # 3. Cache prior-week game stats for point-in-time reconstruction.
    #    The week range must GROW with the season. It was hardcoded (1, 2, 3), which is
    #    exactly right while week 4 is the newest completed week — but from week 5 onward it
    #    would silently deny every reconstruction the in-season stats it is entitled to
    #    (prior weeks only), making the model look progressively staler as the season ran on
    #    while the harness reported nothing wrong. Derive it from the completed games.
    completed_weeks = sorted({int(g.get("week") or 0) for g in games_raw
                              if g.get("homeScore") is not None
                              and g.get("awayScore") is not None})
    past_weeks = tuple(range(1, max(completed_weeks))) if completed_weeks else ()
    if past_weeks:
        print(f"[Backtest] point-in-time stats: weeks {past_weeks[0]}-{past_weeks[-1]} "
              f"(latest completed week is {max(completed_weeks)})")
    else:
        print("[Backtest] no completed weeks yet — week 1 uses the frozen preseason baseline")
    weekly_game_stats = build_weekly_stats_cache(client, weeks=past_weeks)

    game_audit_log = []
    
    tiers = {
        "all_fbs_picks": {"min_edge": 0.5, "w": 0, "l": 0, "p": 0},
        "tier_1_2pt": {"min_edge": 2.0, "w": 0, "l": 0, "p": 0},
        "tier_2_3star_3.5pt": {"min_edge": 3.5, "w": 0, "l": 0, "p": 0},
        "tier_3_5star_7pt": {"min_edge": 7.0, "w": 0, "l": 0, "p": 0},
    }
    
    totals_tiers = {
        "all_totals": {"min_edge": 0.5, "w": 0, "l": 0, "p": 0},
        "totals_3star_4pt": {"min_edge": 4.0, "w": 0, "l": 0, "p": 0},
        "totals_5star_7pt": {"min_edge": 7.0, "w": 0, "l": 0, "p": 0},
    }
    
    by_week = {}

    for g in games_raw:
        h_name = g.get("homeTeam")
        a_name = g.get("awayTeam")
        h_score = g.get("homeScore")
        a_score = g.get("awayScore")
        wk = g.get("week")
        kickoff = g.get("startDate")
        
        # Must be completed
        if h_score is None or a_score is None:
            continue
            
        home_pit = reconstruct_pit_team_data(preseason_map, weekly_game_stats, h_name, wk)
        away_pit = reconstruct_pit_team_data(preseason_map, weekly_game_stats, a_name, wk)
        if not home_pit or not away_pit:
            continue
            
        is_fbs_matchup = (home_pit.get("sp_plus", 0) != 0 and away_pit.get("sp_plus", 0) != 0)

        # 1. Closing-line selection rule with strict timestamp and provider provenance
        line_chosen = None
        for prov in PROVIDER_PREFERENCE:
            for l in g.get("lines", []):
                if l.get("provider") == prov and l.get("spread") is not None:
                    line_chosen = l
                    break
            if line_chosen:
                break
                
        if not line_chosen:
            continue

        closing_spread = float(line_chosen["spread"])
        spread_open = float(line_chosen["spreadOpen"]) if line_chosen.get("spreadOpen") is not None else None
        closing_total = float(line_chosen["overUnder"]) if line_chosen.get("overUnder") is not None else None
        total_open = float(line_chosen["overUnderOpen"]) if line_chosen.get("overUnderOpen") is not None else None
        provider = line_chosen.get("provider")

        # 2. Model projection strictly using reconstructed point-in-time team data
        h2h = app.project_head_to_head(home_pit, away_pit)
        model_spread = h2h["differential"] # home_proj - away_proj
        model_total = h2h["total"]
        
        # Book expected home margin is -spread (e.g. -7.0 spread means home expected to win by 7)
        book_home_margin = -closing_spread
        actual_home_margin = h_score - a_score
        actual_total = h_score + a_score
        
        ats_edge = abs(model_spread - book_home_margin)
        pick_home = model_spread > book_home_margin
        actual_cover_margin = actual_home_margin - book_home_margin
        
        # ATS Outcome
        if actual_cover_margin == 0:
            ats_res = "PUSH"
        elif (pick_home and actual_cover_margin > 0) or (not pick_home and actual_cover_margin < 0):
            ats_res = "WIN"
        else:
            ats_res = "LOSS"

        # Totals Outcome
        ou_res = "N/A"
        ou_edge = 0.0
        if closing_total is not None:
            ou_edge = abs(model_total - closing_total)
            pick_over = model_total > closing_total
            total_margin = actual_total - closing_total
            if total_margin == 0:
                ou_res = "PUSH"
            elif (pick_over and total_margin > 0) or (not pick_over and total_margin < 0):
                ou_res = "WIN"
            else:
                ou_res = "LOSS"
            
        # Record in FBS tiers
        if is_fbs_matchup:
            for t_name, t_data in tiers.items():
                if ats_edge >= t_data["min_edge"]:
                    if ats_res == "WIN":
                        t_data["w"] += 1
                    elif ats_res == "LOSS":
                        t_data["l"] += 1
                    elif ats_res == "PUSH":
                        t_data["p"] += 1

            if closing_total is not None:
                for t_name, t_data in totals_tiers.items():
                    if ou_edge >= t_data["min_edge"]:
                        if ou_res == "WIN":
                            t_data["w"] += 1
                        elif ou_res == "LOSS":
                            t_data["l"] += 1
                        elif ou_res == "PUSH":
                            t_data["p"] += 1

            if wk not in by_week:
                by_week[wk] = {
                    "total_games": 0,
                    "all_fbs": {"w": 0, "l": 0, "p": 0},
                    "3star": {"w": 0, "l": 0, "p": 0},
                    "5star": {"w": 0, "l": 0, "p": 0},
                }
            by_week[wk]["total_games"] += 1
            by_week[wk]["all_fbs"]["w" if ats_res == "WIN" else ("l" if ats_res == "LOSS" else "p")] += 1
            if ats_edge >= 3.5:
                by_week[wk]["3star"]["w" if ats_res == "WIN" else ("l" if ats_res == "LOSS" else "p")] += 1
            if ats_edge >= 7.0:
                by_week[wk]["5star"]["w" if ats_res == "WIN" else ("l" if ats_res == "LOSS" else "p")] += 1

        game_audit_log.append({
            "game_id": g.get("id"),
            "week": wk,
            "home": h_name,
            "away": a_name,
            "is_fbs_matchup": is_fbs_matchup,
            "final_score": f"{h_name} {h_score} - {a_name} {a_score}",
            "closing_timestamp": kickoff,
            "closing_provider": provider,
            "closing_spread": closing_spread,
            "spread_open": spread_open,
            "closing_total": closing_total,
            "total_open": total_open,
            "line_source": f"{provider} closing line frozen at kickoff ({kickoff})",
            "model_proj_spread": model_spread,
            "model_proj_total": model_total,
            "ats_edge": round(ats_edge, 1),
            "ats_pick": f"{h_name} ({closing_spread:+})" if pick_home else f"{a_name} ({-closing_spread:+})",
            "ats_result": ats_res,
            "ou_edge": round(ou_edge, 1),
            "ou_result": ou_res,
        })

    summary = {
        "season": year,
        "closing_line_provenance": "Explicit provider hierarchy (DraftKings primary, Bovada secondary) with kickoff closing timestamp.",
        "point_in_time_isolation": "Strict prior-week reconstruction: week 1 uses frozen preseason baseline; weeks > 1 only ingest stats from completed games (past_wk < week), with zero future leakage.",
        "pit_stat_weeks": list(past_weeks),
        "fcs_exclusion_applied": True,
        "total_evaluated_games": len(game_audit_log),
        "fbs_matchups_evaluated": sum(1 for g in game_audit_log if g["is_fbs_matchup"]),
        "fbs_ats_tiers": {},
        "fbs_totals_tiers": {},
        "walk_forward_weeks": {},
    }
    
    for k, v in tiers.items():
        decided = v["w"] + v["l"]
        pct = round(v["w"] / decided * 100, 1) if decided else 0.0
        summary["fbs_ats_tiers"][k] = {
            "record": f"{v['w']}-{v['l']}-{v['p']}",
            "win_pct": pct,
            "decided_games": decided,
            "min_edge": v["min_edge"],
        }
        
    for k, v in totals_tiers.items():
        decided = v["w"] + v["l"]
        pct = round(v["w"] / decided * 100, 1) if decided else 0.0
        summary["fbs_totals_tiers"][k] = {
            "record": f"{v['w']}-{v['l']}-{v['p']}",
            "win_pct": pct,
            "decided_games": decided,
            "min_edge": v["min_edge"],
        }
        
    for wk_num, wk_d in sorted(by_week.items()):
        wall, lall, pall = wk_d["all_fbs"]["w"], wk_d["all_fbs"]["l"], wk_d["all_fbs"]["p"]
        decall = wall + lall
        pctall = round(wall / decall * 100, 1) if decall else 0.0

        w3, l3, p3 = wk_d["3star"]["w"], wk_d["3star"]["l"], wk_d["3star"]["p"]
        dec3 = w3 + l3
        pct3 = round(w3 / dec3 * 100, 1) if dec3 else 0.0
        
        w5, l5, p5 = wk_d["5star"]["w"], wk_d["5star"]["l"], wk_d["5star"]["p"]
        dec5 = w5 + l5
        pct5 = round(w5 / dec5 * 100, 1) if dec5 else 0.0
        
        summary["walk_forward_weeks"][f"week_{wk_num}"] = {
            "total_fbs_games": wk_d["total_games"],
            "all_fbs_record": f"{wall}-{lall}-{pall} ({pctall}%)",
            "3star_record": f"{w3}-{l3}-{p3} ({pct3}%)",
            "5star_record": f"{w5}-{l5}-{p5} ({pct5}%)",
        }

    _summary_out = Path(summary_out) if summary_out else SUMMARY_FILE
    _fixture_out = Path(fixture_out) if fixture_out else FIXTURE_FILE
    with open(_fixture_out, "w", encoding="utf-8") as f:
        json.dump(game_audit_log, f, indent=2)
        
    with open(_summary_out, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
        
    print(f"[Backtest complete] Saved {len(game_audit_log)} game audits to {_fixture_out}")
    print(f"[Backtest complete] Summary wrote to {_summary_out}")
    return summary

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(
        description="Point-in-time walk-forward backtest. Weights honour "
                    "COMPOSITE_WEIGHTS_JSON, so an alternative weighting can be "
                    "measured without a code change.")
    # Defaults keep the published artifacts as the destination; pass an explicit path
    # when running an EXPERIMENT so the baseline files cannot be overwritten.
    ap.add_argument("--out-summary", default=None,
                    help=f"summary destination (default {SUMMARY_FILE.name})")
    ap.add_argument("--out-fixture", default=None,
                    help=f"game-audit destination (default {FIXTURE_FILE.name})")
    ap.add_argument("--label", default="", help="printed with the result, e.g. 'zero-srs'")
    ap.add_argument("--refresh", action="store_true",
                    help="re-fetch the cached CFBD inputs instead of reusing them")
    args = ap.parse_args()

    if args.refresh:
        REFRESH = True
        print("[Backtest] --refresh: re-fetching cached inputs")

    if args.label:
        print(f"[Backtest] run label: {args.label}")
    print(f"[Backtest] COMPOSITE_WEIGHTS_JSON: {os.environ.get('COMPOSITE_WEIGHTS_JSON') or '<defaults>'}")
    s = run_backtest(summary_out=args.out_summary, fixture_out=args.out_fixture)
    print(json.dumps(s, indent=2))
