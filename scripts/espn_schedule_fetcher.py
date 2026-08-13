#!/usr/bin/env python3
"""Fetch weekly CFB schedule from ESPN API and merge with our analytics data."""
import json, urllib.request, os
from datetime import datetime

ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard"
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Load our analytics data for SP+ and records
analytics_path = os.path.join(BASE_DIR, "data", "analytics.json")
with open(analytics_path) as f:
    analytics_data = json.load(f)

# Build lookup: school_name -> analytics record
team_lookup = {}
for team in analytics_data:
    team_lookup[team["name"].lower()] = team

# Load team name mapping
names_path = os.path.join(BASE_DIR, "data", "team_names.json")
with open(names_path) as f:
    NAME_MAP = json.load(f)

def resolve_name(location):
    """Map ESPN location to our team name."""
    if location in NAME_MAP:
        return NAME_MAP[location]
    # Try case-insensitive
    key = location.lower().strip()
    for espn_name, our_name in NAME_MAP.items():
        if espn_name.lower() == key:
            return our_name
    return None

def get_analytics(name):
    """Get analytics for a team name."""
    return team_lookup.get(name.lower(), {})

def fetch_week(week=1, year=2026):
    """Fetch schedule from ESPN API."""
    url = f"{ESPN_URL}?year={year}&week={week}"
    with urllib.request.urlopen(url) as r:
        return json.loads(r.read())

def build_matchups(data):
    """Build matchup list from ESPN events."""
    matchups = []
    for e in data.get("events", []):
        comp = e.get("competitions", [{}])[0]
        home_team = away_team = None
        for c in comp.get("competitors", []):
            loc = c.get("team", {}).get("location", "")
            if c.get("homeAway") == "home":
                home_team = resolve_name(loc)
            else:
                away_team = resolve_name(loc)
        if not home_team or not away_team:
            continue
        home_ana = get_analytics(home_team)
        away_ana = get_analytics(away_team)
        # Project scores from SP+ (base 21 + sp*15, clamped)
        home_sp = home_ana.get("sp_plus", 0.5)
        away_sp = away_ana.get("sp_plus", 0.5)
        home_proj = max(7, min(63, int(21 + home_sp * 15)))
        away_proj = max(7, min(63, int(21 + away_sp * 15)))
        matchups.append({
            "home": home_team,
            "away": away_team,
            "home_proj": home_proj,
            "away_proj": away_proj,
        })
    return matchups

if __name__ == "__main__":
    week = int(os.environ.get("WEEK", 1))
    data = fetch_week(week)
    matchups = build_matchups(data)
    resolved = sum(1 for m in matchups if m["home"] and m["away"])
    print(f"Week {week}: {len(matchups)} matchups, {resolved} with known teams")
    for m in matchups[:10]:
        print(f"  {m['home']} ({m['home_proj']}) vs {m['away']} ({m['away_proj']})")
    print(f"  ... and {len(matchups)-10} more")
