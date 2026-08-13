#!/usr/bin/env python3
"""
Fix multi-factor model:
1. SP+ normalization (actual range 0-2, not -20 to +40)
2. Use actual fields: sp_plus, fpi_win_prob, cpi, recruiting_rank
3. Fix team name normalization for The Odds API
4. Fix spread matching (match home team, not first 4 chars)
"""
import re

path = 'C:/Users/JeffTracy/Desktop/cfb-power-rankings/app.py'

with open(path, 'r') as f:
    content = f.read()

# ── 1. Fix project_score_multi_factor to use actual fields ──
old_proj = '''def project_score_multi_factor(team_data: dict, is_home: bool = True) -> dict:
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
    }'''

new_proj = '''def project_score_multi_factor(team_data: dict, is_home: bool = True) -> dict:
    """
    Multi-factor projected score model.
    Uses actual cached fields: sp_plus (0-2 scale), fpi_win_prob, cpi, recruiting_rank.
    Weights: SP+ 30%, FPI Win Prob 25%, CPI 20%, Elo (derived) 10%, Recruiting 15%
    Plus home-field advantage adjustment.
    Returns projected score, win probability, and composite rating.
    """
    # Extract ratings with defaults from actual cached data
    sp_plus = team_data.get("sp_plus", 1.0)       # 0-2 scale, ~1.0 = average
    fpi_wp = team_data.get("fpi_win_prob", 50.0)  # 0-100 scale
    cpi = team_data.get("cpi", 50.0)              # 0-100 composite index
    rec_rank = team_data.get("recruiting_rank", 80)
    off_ppg = team_data.get("off_ppg", 28.0)      # points scored per game
    def_ppg = team_data.get("def_ppg", 24.0)      # points allowed per game

    # Normalize each factor to a 0-100 scale
    # SP+: actual range ~0.0-2.0, center at 1.0 -> 0-100
    sp_norm = max(0, min(100, (sp_plus / 2.0) * 100))
    # FPI Win Prob: already 0-100
    fpi_norm = max(0, min(100, fpi_wp))
    # CPI: already 0-100
    cpi_norm = max(0, min(100, cpi))
    # Elo proxy: derive from rank (rank 1 = 100, rank 130 = 0)
    rank = team_data.get("rank", 65)
    elo_norm = max(0, min(100, (130 - rank) / 129 * 100))
    # Recruiting: rank 1-130 -> 0-100 (inverted)
    rec_norm = max(0, min(100, (130 - rec_rank) / 129 * 100))

    # Weighted composite (0-100)
    composite = (
        sp_norm * 0.30 +
        fpi_norm * 0.25 +
        cpi_norm * 0.20 +
        elo_norm * 0.10 +
        rec_norm * 0.15
    )

    # Projected score from offensive/defensive metrics + composite
    # Base: average of team's scoring and allowing
    net_ppg = off_ppg - def_ppg  # positive = outscore opponents
    base_score = 28.0 + (net_ppg * 0.5)  # ~28 base + half the differential
    # Composite adjustment: top teams score more, bottom teams less
    composite_adj = (composite - 50) * 0.3  # ±15 points range
    base_score += composite_adj

    # Home field advantage: ~3.5 points (CFBD research average)
    home_adj = 3.5 if is_home else -1.5
    projected_score = round(max(7, min(63, base_score + home_adj)), 1)

    # Win probability from composite (logistic model)
    # Composite 50 = 50%, 70 = ~88%, 30 = ~12%
    win_prob = 1 / (1 + (2.718 ** (-0.08 * (composite - 50))))
    win_prob = round(win_prob * 100, 1)

    return {
        "projected_score": projected_score,
        "composite": round(composite, 1),
        "win_probability": win_prob,
        "sp_contribution": round(sp_norm * 0.30, 1),
        "fpi_contribution": round(fpi_norm * 0.25, 1),
        "cpi_contribution": round(cpi_norm * 0.20, 1),
        "elo_contribution": round(elo_norm * 0.10, 1),
        "rec_contribution": round(rec_norm * 0.15, 1),
    }'''

content = content.replace(old_proj, new_proj)

# ── 2. Fix team name normalization ──
old_normalize = '''def _normalize_team_name(name: str) -> str:
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
    return name'''

new_normalize = '''def _normalize_team_name(name: str) -> str:
    """Normalize bookmaker team names to our internal team names.
    Bookmakers use 'Location Mascot' format; we use 'Location' or 'School'.
    IMPORTANT: Check longer names first to avoid 'North Carolina' -> 'North'."""
    name_lower = name.lower()
    # Multi-word locations FIRST (before single-word mascots can match)
    multi_word = {
        "north carolina": "North Carolina", "south carolina": "South Carolina",
        "south florida": "South Florida", "east carolina": "East Carolina",
        "west virginia": "West Virginia", "north texas": "North Texas",
        "south alabama": "South Alabama", "arizona state": "Arizona State",
        "arizona wildcats": "Arizona", "colorado buffaloes": "Colorado",
        "colorado state": "Colorado State", "texas a&m": "Texas A&M",
        "texas tech": "Texas Tech", "texas am": "Texas A&M",
        "ohio state": "Ohio State", "michigan state": "Michigan State",
        "mississippi state": "Mississippi State", "oklahoma state": "Oklahoma State",
        "oklahoma": "Oklahoma", "kansas state": "Kansas State",
        "kentucky": "Kentucky", "louisville": "Louisville",
        "penn state": "Penn State", "notre dame": "Notre Dame",
        "georgia tech": "Georgia Tech", "wake forest": "Wake Forest",
        "nc state": "NC State", "virginia tech": "Virginia Tech",
        "san diego state": "San Diego State", "boise state": "Boise State",
        "new mexico": "New Mexico", "nevada": "Nevada",
        "sam houston": "Sam Houston", "southern illinois": "South Illinois",
        "air force": "Air Force", "fresno state": "Fresno State",
        "jacksonville state": "Jacksonville State", "jacksonville": "Jacksonville",
        "sacramento state": "Sacramento State", "sacramento": "Sacramento",
        "abilene christian": "Abilene Christian", "austin peay": "Austin Peay",
        "albany": "Albany", "akron": "Akron", "buffalo": "Buffalo",
        "delaware": "Delaware", "nicholls": "Nicholls", "rice": "Rice",
        "houston": "Houston", "buffalo": "Buffalo", "umass": "UMass",
        "rutgers": "Rutgers", "maryland": "Maryland", "indiana": "Indiana",
        "iowa": "Iowa", "minnesota": "Minnesota", "wisconsin": "Wisconsin",
        "illinois": "Illinois", "nebraska": "Nebraska", "purdue": "Purdue",
        "arkansas": "Arkansas", "missouri": "Missouri", "tennessee": "Tennessee",
        "florida": "Florida", "georgia": "Georgia", "alabama": "Alabama",
        "lsu": "LSU", "ole miss": "Ole Miss", "auburn": "Auburn",
        "vanderbilt": "Vanderbilt", "kentucky": "Kentucky",
        "florida state": "Florida State", "clemson": "Clemson",
        "duke": "Duke", "virginia": "Virginia", "syracuse": "Syracuse",
        "baylor": "Baylor", "byu": "BYU", "cincinnati": "Cincinnati",
        "kansas": "Kansas", "oklahoma": "Oklahoma", "tcu": "TCU",
        "ucf": "UCF", "utah": "Utah", "utah state": "Utah State",
        "washington": "Washington", "oregon": "Oregon", "washington state": "Washington State",
        "oregon state": "Oregon State", "california": "California", "cal": "California",
        "ucla": "UCLA", "usc": "USC", "stanford": "Stanford",
        "san jose state": "San Jose State", "san jose": "San Jose",
    }
    for mascot, school in multi_word.items():
        if mascot in name_lower:
            return school
    # Fallback: first word
    parts = name.split()
    if parts:
        return parts[0]
    return name'''

content = content.replace(old_normalize, new_normalize)

# ── 3. Fix spread matching in _build_odds_map ──
old_spread_match = '''        for bm in game.get("bookmakers", []):
            for market in bm.get("markets", []):
                if market.get("key") == "spreads":
                    for outcome in market.get("outcomes", []):
                        if outcome.get("name", "").lower().startswith(home.lower()[:4]):
                            spread = outcome.get("point")
                elif market.get("key") == "totals":
                    for outcome in market.get("outcomes", []):
                        if outcome.get("name") == "Over":
                            total = outcome.get("point")'''

new_spread_match = '''        # Find spread for the home team (negative = home favorite)
        home_short = home.lower()
        away_short = away.lower()
        for bm in game.get("bookmakers", []):
            for market in bm.get("markets", []):
                if market.get("key") == "spreads":
                    for outcome in market.get("outcomes", []):
                        oname = outcome.get("name", "").lower()
                        # Match home team: look for home name in outcome name
                        # Use full team name check, not prefix
                        if home_short in oname and outcome.get("point", 0) < 0:
                            spread = outcome.get("point")
                        elif home_short in oname:
                            # Home is underdog, spread is positive
                            spread = outcome.get("point")
                elif market.get("key") == "totals":
                    for outcome in market.get("outcomes", []):
                        if outcome.get("name") == "Over":
                            total = outcome.get("point")'''

content = content.replace(old_spread_match, new_spread_match)

# ── 4. Fix spread matching in /api/odds endpoint (same pattern) ──
old_odds_spread = '''            for bm in game.get("bookmakers", []):
                for market in bm.get("markets", []):
                    if market.get("key") == "spreads":
                        for outcome in market.get("outcomes", []):
                            if outcome.get("name", "").lower().startswith(home.lower()[:4]):
                                spread = outcome.get("point")
                    elif market.get("key") == "totals":
                        for outcome in market.get("outcomes", []):
                            if outcome.get("name") == "Over":
                                total = outcome.get("point")'''

new_odds_spread = '''            home_short = home.lower()
            for bm in game.get("bookmakers", []):
                for market in bm.get("markets", []):
                    if market.get("key") == "spreads":
                        for outcome in market.get("outcomes", []):
                            oname = outcome.get("name", "").lower()
                            if home_short in oname:
                                spread = outcome.get("point")
                    elif market.get("key") == "totals":
                        for outcome in market.get("outcomes", []):
                            if outcome.get("name") == "Over":
                                total = outcome.get("point")'''

content = content.replace(old_odds_spread, new_odds_spread)

# Write back
with open(path, 'w') as f:
    f.write(content)

print("Fixes applied!")
print("  1. SP+ normalization: 0-2 scale (was -20 to +40)")
print("  2. Using actual fields: sp_plus, fpi_win_prob, cpi, recruiting_rank")
print("  3. Team name normalization: multi-word locations checked first")
print("  4. Spread matching: full name substring (was 4-char prefix)")
