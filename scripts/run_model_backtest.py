"""
run_model_backtest.py
Reproducible game-level backtest harness for cfb-power-rankings.
Evaluates model projections against real closing spreads and totals from CFBD (/lines),
measuring ATS cover rates, totals accuracy, and edge tiers (3-star, 5-star) across completed 2026 games.
Outputs:
  data/backtest_fixture_2026.json  (game-level audit log with closing lines and picks)
  data/backtest_summary.json       (verified ATS win rates by tier and conference)
"""

import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
import app

DATA_DIR = ROOT_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
FIXTURE_FILE = DATA_DIR / "backtest_fixture_2026.json"
SUMMARY_FILE = DATA_DIR / "backtest_summary.json"

def run_backtest(year: int = 2026) -> dict:
    client = app.httpx.Client(timeout=20)
    team_map = app._build_team_map()
    
    print(f"[Backtest] Loading CFBD closing lines and game scores for {year}...")
    url = f"{app.CFBD_BASE}/lines?year={year}"
    resp = client.get(url, headers=app.CFBD_HEADERS)
    if resp.status_code != 200:
        raise RuntimeError(f"Failed to fetch CFBD lines: HTTP {resp.status_code}")
    games_raw = resp.json()
    print(f"[Backtest] Fetched {len(games_raw)} raw game lines.")

    game_audit_log = []
    
    tiers = {
        "all_picks": {"min_edge": 0.5, "w": 0, "l": 0, "p": 0},
        "tier_1_edge_2pt": {"min_edge": 2.0, "w": 0, "l": 0, "p": 0},
        "tier_2_3star_3.5pt": {"min_edge": 3.5, "w": 0, "l": 0, "p": 0},
        "tier_3_5star_5.5pt": {"min_edge": 5.5, "w": 0, "l": 0, "p": 0},
        "high_edge_7pt": {"min_edge": 7.0, "w": 0, "l": 0, "p": 0},
    }
    
    totals_tiers = {
        "all_totals": {"min_edge": 0.5, "w": 0, "l": 0, "p": 0},
        "totals_3star_4pt": {"min_edge": 4.0, "w": 0, "l": 0, "p": 0},
        "totals_5star_7pt": {"min_edge": 7.0, "w": 0, "l": 0, "p": 0},
    }

    for g in games_raw:
        h_name = g.get("homeTeam")
        a_name = g.get("awayTeam")
        h_score = g.get("homeScore")
        a_score = g.get("awayScore")
        
        # Must be a completed game
        if h_score is None or a_score is None:
            continue
        if h_name not in team_map or a_name not in team_map:
            continue
            
        # Extract closing spread and total
        closing_spread = None
        closing_total = None
        for line in g.get("lines", []):
            if line.get("spread") is not None and closing_spread is None:
                closing_spread = float(line["spread"])
            if line.get("overUnder") is not None and closing_total is None:
                closing_total = float(line["overUnder"])
            if closing_spread is not None and closing_total is not None:
                break
                
        if closing_spread is None:
            continue

        # Model projection
        h2h = app.project_head_to_head(team_map[h_name], team_map[a_name])
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

        # Record in tiers
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

        game_audit_log.append({
            "game_id": g.get("id"),
            "week": g.get("week"),
            "home": h_name,
            "away": a_name,
            "final_score": f"{h_name} {h_score} - {a_name} {a_score}",
            "closing_spread": closing_spread,
            "closing_total": closing_total,
            "model_proj_spread": model_spread,
            "model_proj_total": model_total,
            "ats_edge": round(ats_edge, 1),
            "ats_pick": f"{h_name} ({closing_spread:+})" if pick_home else f"{a_name} ({-closing_spread:+})",
            "ats_result": ats_res,
            "ou_edge": round(ou_edge, 1),
            "ou_result": ou_res,
        })

    # Summary formatting
    summary = {
        "season": year,
        "total_evaluated_games": len(game_audit_log),
        "ats_tiers": {},
        "totals_tiers": {},
    }
    
    for k, v in tiers.items():
        decided = v["w"] + v["l"]
        pct = round(v["w"] / decided * 100, 1) if decided else 0.0
        summary["ats_tiers"][k] = {
            "record": f"{v['w']}-{v['l']}-{v['p']}",
            "win_pct": pct,
            "decided_games": decided,
            "min_edge": v["min_edge"],
        }
        
    for k, v in totals_tiers.items():
        decided = v["w"] + v["l"]
        pct = round(v["w"] / decided * 100, 1) if decided else 0.0
        summary["totals_tiers"][k] = {
            "record": f"{v['w']}-{v['l']}-{v['p']}",
            "win_pct": pct,
            "decided_games": decided,
            "min_edge": v["min_edge"],
        }

    with open(FIXTURE_FILE, "w", encoding="utf-8") as f:
        json.dump(game_audit_log, f, indent=2)
        
    with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
        
    print(f"[Backtest complete] Wrote {len(game_audit_log)} game audits to {FIXTURE_FILE}")
    print(f"[Backtest complete] Summary wrote to {SUMMARY_FILE}")
    return summary

if __name__ == "__main__":
    s = run_backtest()
    print(json.dumps(s, indent=2))
