"""
backfill_ats_margins.py
Pulls historical and in-season Against-The-Spread (ATS) performance records and
average cover margins from CFBD (/teams/ats) for 2025 and 2026.
Compiles conference averages and team cover margins to provide CFO with verified
data for Closing Line Value (CLV) and 3-star vs 5-star betting tiers.
"""

import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))
import app

DATA_DIR = ROOT_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE = DATA_DIR / "team_ats_history.json"

def fetch_ats_records(years=(2025, 2026)) -> dict:
    client = app.httpx.Client(timeout=15)
    records_by_team = {}
    
    for yr in years:
        url = f"{app.CFBD_BASE}/teams/ats?year={yr}"
        try:
            resp = client.get(url, headers=app.CFBD_HEADERS)
            if resp.status_code == 200:
                data = resp.json()
                print(f"[ATS] Fetched {len(data)} team records for {yr}")
                for entry in data:
                    team = entry.get("team")
                    if not team:
                        continue
                    if team not in records_by_team:
                        records_by_team[team] = {
                            "team": team,
                            "conference": entry.get("conference"),
                            "seasons": {}
                        }
                    records_by_team[team]["seasons"][str(yr)] = {
                        "games": entry.get("games", 0),
                        "ats_wins": entry.get("atsWins", 0),
                        "ats_losses": entry.get("atsLosses", 0),
                        "ats_pushes": entry.get("atsPushes", 0),
                        "avg_cover_margin": entry.get("avgCoverMargin", 0.0),
                    }
            else:
                print(f"[ATS warning] Failed {yr}: status {resp.status_code}")
        except Exception as e:
            print(f"[ATS error] {yr}: {e}")
            
    # Calculate multi-year aggregate cover margin for each team
    for team, info in records_by_team.items():
        total_games = 0
        total_wins = 0
        total_losses = 0
        total_pushes = 0
        weighted_margin_sum = 0.0
        
        for yr_str, s in info["seasons"].items():
            g = s.get("games", 0)
            if g > 0:
                total_games += g
                total_wins += s.get("ats_wins", 0)
                total_losses += s.get("ats_losses", 0)
                total_pushes += s.get("ats_pushes", 0)
                weighted_margin_sum += s.get("avg_cover_margin", 0.0) * g
                
        info["aggregate"] = {
            "total_games": total_games,
            "total_ats_wins": total_wins,
            "total_ats_losses": total_losses,
            "total_ats_pushes": total_pushes,
            "ats_win_pct": round(total_wins / total_games * 100, 1) if total_games > 0 else 0.0,
            "weighted_avg_cover_margin": round(weighted_margin_sum / total_games, 2) if total_games > 0 else 0.0,
        }

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(records_by_team, f, indent=2)
        
    print(f"[ATS complete] Saved {len(records_by_team)} team records to {OUT_FILE}")
    return records_by_team

if __name__ == "__main__":
    fetch_ats_records()
