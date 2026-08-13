#!/usr/bin/env python3
"""
Fix odds endpoint:
1. Sort team name normalization by length descending (longer matches first)
2. Fix spread matching: compare against bookmaker team names, not normalized names
3. Add missing team mappings for bookmaker names
"""

path = 'C:/Users/JeffTracy/Desktop/cfb-power-rankings/app.py'

with open(path, 'r') as f:
    content = f.read()

# Fix the /api/odds endpoint to use bookmaker names for spread matching
# and normalize AFTER matching
old_odds = '''        for game in the_odds:
            home = _normalize_team_name(game.get("home_team", ""))
            away = _normalize_team_name(game.get("away_team", ""))
            key = f"{home}@{away}"
            spread = None
            total = None
            home_short = home.lower()
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
                                total = outcome.get("point")
            merged[key] = {
                "home": home,
                "away": away,
                "spread": round(spread, 1) if spread else None,
                "total": round(total, 1) if total else None,
                "source": "the_odds_api",
            }'''

new_odds = '''        for game in the_odds:
            home_raw = game.get("home_team", "")
            away_raw = game.get("away_team", "")
            home = _normalize_team_name(home_raw)
            away = _normalize_team_name(away_raw)
            key = f"{home}@{away}"
            spread = None
            total = None
            # Match against RAW bookmaker names (not normalized)
            home_bm = home_raw.lower()
            for bm in game.get("bookmakers", []):
                for market in bm.get("markets", []):
                    if market.get("key") == "spreads":
                        for outcome in market.get("outcomes", []):
                            oname = outcome.get("name", "").lower()
                            if home_bm in oname:
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
            }'''

content = content.replace(old_odds, new_odds)

# Fix team name normalization: sort by length descending so "florida state" beats "florida"
old_normalize_start = '''def _normalize_team_name(name: str) -> str:
    """Normalize bookmaker team names to our internal team names.
    Bookmakers use 'Location Mascot' format; we use 'Location' or 'School'.
    IMPORTANT: Check longer names first to avoid 'North Carolina' -> 'North'."""
    name_lower = name.lower()
    # Multi-word locations FIRST (before single-word mascots can match)
    multi_word = {'''

new_normalize_start = '''def _normalize_team_name(name: str) -> str:
    """Normalize bookmaker team names to our internal team names.
    Bookmakers use 'Location Mascot' format; we use 'Location' or 'School'.
    IMPORTANT: Check longer names first to avoid 'North Carolina' -> 'North'."""
    name_lower = name.lower()
    # Sorted list of (pattern, school) - longer patterns checked FIRST
    _name_map = [
        # 3+ word teams first
        ("jacksonville state", "Jacksonville State"),
        ("sacramento state", "Sacramento State"),
        ("san jose state", "San Jose State"),
        ("washington state", "Washington State"),
        ("oregon state", "Oregon State"),
        ("utah state", "Utah State"),
        ("east carolina", "East Carolina"),
        ("abilene christian", "Abilene Christian"),
        ("austin peay", "Austin Peay"),
        ("southern miss", "Southern Miss"),
        ("southern illinois", "Southern Illinois"),
        ("bethune-cookman", "Bethune-Cookman"),
        ("north carolina central", "NC Central"),
        # 2-word teams (location-based)
        ("florida state", "Florida State"),
        ("georgia tech", "Georgia Tech"),
        ("ohio state", "Ohio State"),
        ("michigan state", "Michigan State"),
        ("penn state", "Penn State"),
        ("north carolina", "North Carolina"),
        ("south carolina", "South Carolina"),
        ("south florida", "South Florida"),
        ("west virginia", "West Virginia"),
        ("north texas", "North Texas"),
        ("south alabama", "South Alabama"),
        ("arizona state", "Arizona State"),
        ("colorado state", "Colorado State"),
        ("texas a&m", "Texas A&M"),
        ("texas am", "Texas A&M"),
        ("texas tech", "Texas Tech"),
        ("mississippi state", "Mississippi State"),
        ("oklahoma state", "Oklahoma State"),
        ("kansas state", "Kansas State"),
        ("san diego state", "San Diego State"),
        ("boise state", "Boise State"),
        ("fresno state", "Fresno State"),
        ("virginia tech", "Virginia Tech"),
        ("wake forest", "Wake Forest"),
        ("notre dame", "Notre Dame"),
        ("ole miss", "Ole Miss"),
        ("air force", "Air Force"),
        ("sam houston", "Sam Houston"),
        # Single-word abbreviations
        ("ucf", "UCF"),
        ("usf", "South Florida"),
        ("usc", "USC"),
        ("ucla", "UCLA"),
        ("lsu", "LSU"),
        ("byu", "BYU"),
        ("tcu", "TCU"),
        ("umass", "UMass"),
        ("ucf", "UCF"),
        # Mascot-based (most specific first)
        ("seminoles", "Florida State"),
        ("yellow jackets", "Georgia Tech"),
        ("tar heels", "North Carolina"),
        ("gamecocks", "South Carolina"),
        ("bulldogs", "Georgia"),
        ("gators", "Florida"),
        ("longhorns", "Texas"),
        ("crimson tide", "Alabama"),
        ("volunteers", "Tennessee"),
        ("wolverines", "Michigan"),
        ("buckeyes", "Ohio State"),
        ("nittany lions", "Penn State"),
        ("hoosiers", "Indiana"),
        ("wildcats", "Kentucky"),
        ("flying mice", "Iowa"),
        ("cardinals", "Stanford"),
        ("sun devils", "Arizona"),
        ("bruins", "UCLA"),
        ("trojans", "USC"),
        ("cardinal", "Notre Dame"),
        ("ducks", "Oregon"),
        ("huskies", "Washington"),
        ("cougars", "Oregon State"),
        ("beavers", "Oregon"),
        ("aggies", "Texas A&M"),
        ("rangers", "Texas Tech"),
        ("broncos", "Boise State"),
        ("miners", "Nevada"),
        ("rebels", "Ole Miss"),
        ("tigers", "Auburn"),
        ("fighters", "BYU"),
        ("bearcats", "Cincinnati"),
        ("orange", "Syracuse"),
        ("minutemen", "UMass"),
        ("rams", "Colorado State"),
        ("mountaineers", "West Virginia"),
        ("cowboys", "TCU"),
        ("frogs", "TCU"),
        ("horned frogs", "TCU"),
        ("jaguars", "Jacksonville State"),
        ("panthers", "Jacksonville"),
        ("blue devils", "Duke"),
        ("deacs", "Wake Forest"),
        ("pirates", "NC State"),
        ("wolfpack", "NC State"),
        ("hokies", "Virginia Tech"),
        ("cavaliers", "Virginia"),
        ("spartans", "Michigan State"),
        ("badgers", "Wisconsin"),
        ("illini", "Illinois"),
        ("fighting illini", "Illinois"),
        ("rushmore", "Rutgers"),
        ("scarlet knights", "Rutgers"),
        ("gophers", "Minnesota"),
        ("rockets", "Arkansas"),
        ("falcons", "Air Force"),
        ("warriors", "Fresno State"),
        ("midshipmen", "Navy"),
        ("knights", "Houston"),
        ("bearkats", "Sam Houston"),
        ("owls", "Rice"),
        ("golden eagles", "Navy"),
        ("eagles", "San Diego State"),
        ("bulls", "Buffalo"),
        ("49ers", "San Francisco"),
    ]
    for pattern, school in _name_map:
        if pattern in name_lower:
            return school
    # Fallback: first word
    parts = name.split()
    if parts:
        return parts[0]
    return name'''

content = content.replace(old_normalize_start, new_normalize_start)

# Remove the old multi_word dict and loop that follows
old_multi_block = '''    multi_word = {
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

new_multi_block = '''    # (handled by _name_map above)
    pass'''

content = content.replace(old_multi_block, new_multi_block)

with open(path, 'w') as f:
    f.write(content)

print("Odds fixes applied!")
