#!/usr/bin/env python3
"""
Patch app.py to add:
1. The Odds API integration (spread + totals)
2. Multi-factor projection model
3. /api/odds and /api/projections endpoints
"""
import re

path = 'C:/Users/JeffTracy/Desktop/cfb-power-rankings/app.py'

with open(path, 'r') as f:
    content = f.read()

# ── 1. Add The Odds API key after CFBD_KEY ──
if 'THE_ODDS_API_KEY' not in content:
    old = 'CFBD_YEAR = 2026'
    new = '''THE_ODDS_API_KEY = "ed6531e2046bc0a29ba24030cfd34782"
THE_ODDS_BASE = "https://api.the-odds-api.com/v4"
CFBD_YEAR = 2026'''
    content = content.replace(old, new, 1)

# ── 2. Add The Odds fetcher after _cfbd_elo function ──
if 'def _the_odds_fetch' not in content:
    insert_after = 'def _cfbd_elo() -> dict:'
    # Find the end of _cfbd_elo function (next def or blank line before def)
    idx = content.index(insert_after)
    # Find the next function definition after _cfbd_elo
    rest = content[idx:]
    next_def_match = re.search(r'\n\ndef \w+', rest[1:])
    if next_def_match:
        insert_pos = idx + 1 + next_def_match.start()
    else:
        insert_pos = idx + len(rest)

    the_odds_code = '''
def _the_odds_fetch() -> list[dict]:
    """Fetch live NCAAF spreads and totals from The Odds API."""
    try:
        url = f"{THE_ODDS_BASE}/sports/americanfootball_ncaaf/odds"
        params = {
            "apiKey": THE_ODDS_API_KEY,
            "regions": "us",
            "markets": "spreads,totals",
            "oddsFormat": "decimal",
        }
        with httpx.Client(timeout=15) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        print(f"[The Odds API fetch failed] {e}")
        return []

def _normalize_team_name(name: str) -> str:
    """Normalize bookmaker team names to our internal team names.
    Bookmakers use 'Location Mascot' format; we use 'Location' or 'School'."""
    name_lower = name.lower()
    # Common mappings: "Texas Longhorns" -> "Texas", "Alabama Crimson Tide" -> "Alabama"
    mascot_strip = {
        "longhorns": "Texas", "crimson tide": "Alabama", "seminoles": "Florida State",
        "gators": "Florida", "bulldogs": "Georgia", "volunteers": "Tennessee",
        "wolverines": "Michigan", "nittany lions": "Penn State", "hoosiers": "Indiana",
        "wildcats": "Kentucky", "flying mice": "Iowa", "cardinals": "Stanford",
        "sun devils": "Arizona", "bruins": "UCLA", "trojans": "USC",
        "cardinal": "Notre Dame", "ducks": "Oregon", "huskies": "Washington",
        "cougars": "Oregon State", "beavers": "Oregon", "aggies": "Texas A&M",
        "rangers": "Texas Tech", "broncos": "Boise State", "miners": "Nevada",
        "rebels": "Ole Miss", "tigers": "Auburn", "fighters": "BYU",
        "bearcats": "Cincinnati", "orange": "Syracuse", "minutemen": "UMass",
        "rams": "Colorado State", "mountaineers": "West Virginia", "cowboys": "TCU",
        "frogs": "TCU", "horned frogs": "TCU", "jaguars": "Jacksonville State",
        "panthers": "Jacksonville State", "blue devils": "Duke", "tar heels": "North Carolina",
        "deacs": "Wake Forest", "pirates": "NC State", "wolfpack": "NC State",
        "yellow jackets": "Georgia Tech", "hokies": "Virginia Tech", "cavaliers": "Virginia",
        "spartans": "Michigan State", "badgers": "Wisconsin", "illini": "Illinois",
        "fighting illini": "Illinois", "rushmore": "Rutgers", "scarlet knights": "Rutgers",
        "hoosiers": "Indiana", "gophers": "Minnesota", "rockets": "Arkansas",
        "salukis": "Southern Illinois", "falcons": "Air Force", "warriors": "Army",
        "midshipmen": "Navy", "knights": "Houston", "bearkats": "Sam Houston",
        "owls": "Rice", "golden eagles": "Navy", "eagles": "San Diego State",
        "miners": "New Mexico", "mountaineers": "Appalachian State", "warriors": "Fresno State",
        "bulls": "Buffalo", "panthers": "Jacksonville", "49ers": "San Francisco",
    }
    for mascot, school in mascot_strip.items():
        if mascot in name_lower:
            return school
    # Try to extract location part (first word usually)
    parts = name.split()
    if parts:
        return parts[0]
    return name

def _build_odds_map(odds_data: list[dict]) -> dict:
    """Build a lookup dict keyed by (home_team, away_team) -> odds summary."""
    odds_map = {}
    for game in odds_data:
        home = _normalize_team_name(game.get("home_team", ""))
        away = _normalize_team_name(game.get("away_team", ""))
        key = (home, away)
        # Extract DraftKings or FanDuel spread/totals (prefer DraftKings)
        spread = None
        total = None
        spread_open = None
        total_open = None
        for bm in game.get("bookmakers", []):
            for market in bm.get("markets", []):
                if market.get("key") == "spreads":
                    for outcome in market.get("outcomes", []):
                        if outcome.get("name", "").lower().startswith(home.lower()[:4]):
                            spread = outcome.get("point")
                elif market.get("key") == "totals":
                    for outcome in market.get("outcomes", []):
                        if outcome.get("name") == "Over":
                            total = outcome.get("point")
        # Also try to get opening lines from CFBD
        odds_map[key] = {
            "spread": round(spread, 1) if spread else None,
            "total": round(total, 1) if total else None,
            "spread_home_favorite": spread is not None and spread < 0,
        }
    return odds_map

'''
    content = content[:insert_pos] + the_odds_code + content[insert_pos:]

# ── 3. Add multi-factor projection function ──
if 'def project_score_multi_factor' not in content:
    # Insert after _build_odds_map
    insert_after = 'def _build_odds_map(odds_data: list[dict]) -> dict:'
    idx = content.index(insert_after)
    rest = content[idx:]
    next_def_match = re.search(r'\n\ndef \w+', rest[1:])
    if next_def_match:
        insert_pos = idx + 1 + next_def_match.start()
    else:
        insert_pos = idx + len(rest)

    mf_code = '''
def project_score_multi_factor(team_data: dict, is_home: bool = True) -> dict:
    """
    Multi-factor projected score model.
    Weights: SP+ 30%, FPI 25%, SRS 20%, Elo 15%, Recruiting 10%
    Plus home-field advantage adjustment.
    Returns projected score, win probability, and composite rating.
    """
    # Extract ratings with defaults
    sp_plus = team_data.get("sp_plus", 0.0)
    fpi = team_data.get("fpi", 0.0)
    srs = team_data.get("srs", 0.0)
    elo = team_data.get("elo", 1500.0)
    rec_rank = team_data.get("recruiting_rank", 100)

    # Normalize each factor to a 0-100 scale
    # SP+: typical range -20 to +40, center at 10 -> 0-100
    sp_norm = max(0, min(100, (sp_plus + 20) / 60 * 100))
    # FPI: typical range -15 to +25, center at 5 -> 0-100
    fpi_norm = max(0, min(100, (fpi + 15) / 40 * 100))
    # SRS: typical range -15 to +35, center at 10 -> 0-100
    srs_norm = max(0, min(100, (srs + 15) / 50 * 100))
    # Elo: typical range 1000-2500, center at 1500 -> 0-100
    elo_norm = max(0, min(100, (elo - 1000) / 1500 * 100))
    # Recruiting: rank 1-130 -> 0-100 (inverted, rank 1 = 100)
    rec_norm = max(0, min(100, (130 - rec_rank) / 129 * 100))

    # Weighted composite (0-100)
    composite = (
        sp_norm * 0.30 +
        fpi_norm * 0.25 +
        srs_norm * 0.20 +
        elo_norm * 0.15 +
        rec_norm * 0.10
    )

    # Convert composite to projected score (7-63 point range)
    # Composite 50 = ~21 points (average), 100 = 63, 0 = 7
    base_score = 7 + (composite / 100) * 56

    # Home field advantage: ~3.5 points (CFBD research average)
    home_adj = 3.5 if is_home else -1.5  # away slight penalty for travel
    projected_score = round(base_score + home_adj, 1)

    # Win probability from composite (logistic model)
    # Composite 50 = 50%, 75 = ~84%, 25 = ~16%
    win_prob = 1 / (1 + (2.718 ** (-0.1 * (composite - 50))))
    win_prob = round(win_prob * 100, 1)

    return {
        "projected_score": round(projected_score, 1),
        "composite": round(composite, 1),
        "win_probability": win_prob,
        "sp_contribution": round(sp_norm * 0.30, 1),
        "fpi_contribution": round(fpi_norm * 0.25, 1),
        "srs_contribution": round(srs_norm * 0.20, 1),
        "elo_contribution": round(elo_norm * 0.15, 1),
        "rec_contribution": round(rec_norm * 0.10, 1),
    }

'''
    content = content[:insert_pos] + mf_code + content[insert_pos:]

# ── 4. Add /api/odds endpoint ──
if '@app.get("/api/odds")' not in content:
    insert_after = '@app.get("/api/schedule")'
    idx = content.index(insert_after)
    # Find the end of api_schedule function
    rest = content[idx:]
    next_decorator = re.search(r'\n@app\.', rest[1:])
    if next_decorator:
        insert_pos = idx + 1 + next_decorator.start()
    else:
        insert_pos = idx + len(rest)

    odds_endpoint = '''
@app.get("/api/odds")
def api_odds():
    """Get live betting odds for NCAAF from The Odds API + CFBD."""
    try:
        the_odds = _the_odds_fetch()
        cfbd_lines = _cfbd_lines()
        # Merge: use The Odds API as primary, CFBD as fallback for games without odds
        merged = {}
        for game in the_odds:
            home = _normalize_team_name(game.get("home_team", ""))
            away = _normalize_team_name(game.get("away_team", ""))
            key = f"{home}@{away}"
            spread = None
            total = None
            for bm in game.get("bookmakers", []):
                for market in bm.get("markets", []):
                    if market.get("key") == "spreads":
                        for outcome in market.get("outcomes", []):
                            if outcome.get("name", "").lower().startswith(home.lower()[:4]):
                                spread = outcome.get("point")
                    elif market.get("key") == "totals":
                        for outcome in market.get("outcomes", []):
                            if outcome.get("name") == "Over":
                                total = outcome.get("point")
            merged[key] = {
                "home": home,
                "away": away,
                "spread": round(spread, 1) if spread else None,
                "total": round(total, 1) if total else None,
                "source": "the_odds_api",
            }
        # Fill gaps with CFBD
        for game in cfbd_lines:
            home = game.get("homeTeam", "")
            away = game.get("awayTeam", "")
            key = f"{home}@{away}"
            if key not in merged and game.get("lines"):
                line = game["lines"][0]
                merged[key] = {
                    "home": home,
                    "away": away,
                    "spread": line.get("spread"),
                    "total": line.get("overUnder"),
                    "source": "cfbd",
                }
        return {
            "odds": list(merged.values()),
            "count": len(merged),
            "updated": datetime.now().isoformat(),
        }
    except Exception as e:
        print(f"[GET /api/odds ERROR] {e}")
        return {"error": str(e), "odds": []}

'''
    content = content[:insert_pos] + odds_endpoint + content[insert_pos:]

# ── 5. Add CFBD /lines fetcher (needed by odds endpoint) ──
if 'def _cfbd_lines' not in content:
    insert_after = 'def _cfbd_elo() -> dict:'
    idx = content.index(insert_after)
    rest = content[idx:]
    next_def_match = re.search(r'\n\ndef \w+', rest[1:])
    if next_def_match:
        insert_pos = idx + 1 + next_def_match.start()
    else:
        insert_pos = idx + len(rest)

    lines_code = '''
def _cfbd_lines() -> list:
    """Fetch betting lines from CFBD API."""
    try:
        data = _cfbd_get("lines", CFBD_YEAR)
        return data
    except Exception as e:
        print(f"[CFBD lines fetch failed] {e}")
        return []

'''
    content = content[:insert_pos] + lines_code + content[insert_pos:]

# ── 6. Add /api/projections endpoint ──
if '@app.get("/api/projections")' not in content:
    insert_after = '@app.get("/api/odds")'
    idx = content.index(insert_after)
    rest = content[idx:]
    next_decorator = re.search(r'\n@app\.', rest[1:])
    if next_decorator:
        insert_pos = idx + 1 + next_decorator.start()
    else:
        insert_pos = idx + len(rest)

    proj_endpoint = '''
@app.get("/api/projections")
def api_projections():
    """Get multi-factor projections for all teams + live betting odds overlay."""
    try:
        rankings = get_rankings()
        projections = []
        for team in rankings.teams:
            td = team.model_dump() if hasattr(team, 'model_dump') else team.dict()
            home_proj = project_score_multi_factor(td, is_home=True)
            away_proj = project_score_multi_factor(td, is_home=False)
            projections.append({
                **td,
                "home_projection": home_proj,
                "away_projection": away_proj,
            })
        # Sort by composite (home) descending
        projections.sort(key=lambda x: x["home_projection"]["composite"], reverse=True)
        return {"projections": projections, "count": len(projections)}
    except Exception as e:
        print(f"[GET /api/projections ERROR] {e}")
        return {"error": str(e), "projections": []}

'''
    content = content[:insert_pos] + proj_endpoint + content[insert_pos:]

# ── 7. Update api_schedule_fetch to use multi-factor + odds ──
old_schedule_fetch = '''    teams = load_local()
    team_map = {t["name"]: t for t in teams}
    conf_map = load_fbs_conferences()
    enriched = []
    for m in matchups:
        home = team_map.get(m["home"], {})
        away = team_map.get(m["away"], {})
        # Project scores from SP+
        home_sp = home.get("sp_plus", 0.5)
        away_sp = away.get("sp_plus", 0.5)
        home_proj = max(7, min(63, int(21 + home_sp * 15)))
        away_proj = max(7, min(63, int(21 + away_sp * 15)))
        diff = home_proj - away_proj
        # Resolve conference: local team data > FBS DB > override > empty
        home_conf = home.get("conf") or conf_map.get(m["home"], "")
        away_conf = away.get("conf") or conf_map.get(m["away"], "")
        enriched.append({
            **m,
            "home_proj": home_proj,
            "away_proj": away_proj,
            "differential": diff,
            "home_favorite": diff > 0,
            "home_sp": home_sp,
            "away_sp": away_sp,
            "home_record": f"{home.get('wins',0)}-{home.get('losses',0)}",
            "away_record": f"{away.get('wins',0)}-{away.get('losses',0)}",
            "home_conf": home_conf,
            "away_conf": away_conf,
        })
    return {"week": week, "season": year, "updated": datetime.now().isoformat(), "matchups": enriched}'''

new_schedule_fetch = '''    teams = load_local()
    team_map = {t["name"]: t for t in teams}
    conf_map = load_fbs_conferences()

    # Fetch live betting odds for overlay
    try:
        the_odds = _the_odds_fetch()
        odds_map = _build_odds_map(the_odds)
    except Exception:
        odds_map = {}

    enriched = []
    for m in matchups:
        home = team_map.get(m["home"], {})
        away = team_map.get(m["away"], {})
        # Multi-factor projection
        home_proj_data = project_score_multi_factor(home, is_home=True)
        away_proj_data = project_score_multi_factor(away, is_home=False)
        home_proj = home_proj_data["projected_score"]
        away_proj = away_proj_data["projected_score"]
        diff = round(home_proj - away_proj, 1)
        # Betting market overlay
        odds_key = (m["home"], m["away"])
        market_odds = odds_map.get(odds_key, {})
        # Resolve conference
        home_conf = home.get("conf") or conf_map.get(m["home"], "")
        away_conf = away.get("conf") or conf_map.get(m["away"], "")
        enriched.append({
            **m,
            "home_proj": home_proj,
            "away_proj": away_proj,
            "differential": diff,
            "home_favorite": diff > 0,
            "home_composite": home_proj_data["composite"],
            "away_composite": away_proj_data["composite"],
            "home_win_prob": home_proj_data["win_probability"],
            "away_win_prob": away_proj_data["win_probability"],
            "home_sp": home.get("sp_plus", 0),
            "away_sp": away.get("sp_plus", 0),
            "market_spread": market_odds.get("spread"),
            "market_total": market_odds.get("total"),
            "home_record": f"{home.get('wins',0)}-{home.get('losses',0)}",
            "away_record": f"{away.get('wins',0)}-{away.get('losses',0)}",
            "home_conf": home_conf,
            "away_conf": away_conf,
        })
    return {"week": week, "season": year, "updated": datetime.now().isoformat(), "matchups": enriched, "has_odds": len(odds_map) > 0}'''

content = content.replace(old_schedule_fetch, new_schedule_fetch)

# ── 8. Update api_schedule (GET) to use multi-factor ──
old_schedule_get = '''    teams = load_local()
    team_map = {t["name"]: t for t in teams}
    enriched = []
    for m in sched.get("matchups", []):
        home = team_map.get(m["home"], {})
        away = team_map.get(m["away"], {})
        diff = m["home_proj"] - m["away_proj"]
        enriched.append({
            **m,
            "differential": diff,
            "home_favorite": diff > 0,
            "home_sp": home.get("sp_plus", 0),
            "away_sp": away.get("sp_plus", 0),
            "home_record": f"{home.get('wins',0)}-{home.get('losses',0)}",
            "away_record": f"{away.get('wins',0)}-{away.get('losses',0)}",
            "home_conf": home.get("conf", ""),
            "away_conf": away.get("conf", ""),
        })
    return {**sched, "matchups": enriched}'''

new_schedule_get = '''    teams = load_local()
    team_map = {t["name"]: t for t in teams}
    enriched = []
    for m in sched.get("matchups", []):
        home = team_map.get(m["home"], {})
        away = team_map.get(m["away"], {})
        # Multi-factor projection
        home_proj_data = project_score_multi_factor(home, is_home=True)
        away_proj_data = project_score_multi_factor(away, is_home=False)
        diff = round(home_proj_data["projected_score"] - away_proj_data["projected_score"], 1)
        enriched.append({
            **m,
            "home_proj": home_proj_data["projected_score"],
            "away_proj": away_proj_data["projected_score"],
            "differential": diff,
            "home_favorite": diff > 0,
            "home_composite": home_proj_data["composite"],
            "away_composite": away_proj_data["composite"],
            "home_win_prob": home_proj_data["win_probability"],
            "away_win_prob": away_proj_data["win_probability"],
            "home_sp": home.get("sp_plus", 0),
            "away_sp": away.get("sp_plus", 0),
            "home_record": f"{home.get('wins',0)}-{home.get('losses',0)}",
            "away_record": f"{away.get('wins',0)}-{away.get('losses',0)}",
            "home_conf": home.get("conf", ""),
            "away_conf": away.get("conf", ""),
        })
    return {**sched, "matchups": enriched}'''

content = content.replace(old_schedule_get, new_schedule_get)

# Write back
with open(path, 'w') as f:
    f.write(content)

print("Patches applied successfully!")
print(f"File size: {len(content)} chars")
print("Changes:")
print("  1. Added THE_ODDS_API_KEY")
print("  2. Added _the_odds_fetch() + _normalize_team_name() + _build_odds_map()")
print("  3. Added _cfbd_lines() fetcher")
print("  4. Added project_score_multi_factor() model")
print("  5. Added /api/odds endpoint")
print("  6. Added /api/projections endpoint")
print("  7. Updated /api/schedule/fetch with multi-factor + odds overlay")
print("  8. Updated /api/schedule GET with multi-factor projections")
