#!/usr/bin/env python3
"""
Automated college football injury scraper.
Scrapes FBS injuries from Covers, maps to CFBD team names,
determines starter & star status, calculates point deductions,
and stores active injuries in data/active_injuries.json.
"""

import os
import sys
import json
import re
import httpx
from datetime import datetime

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data"))
ACTIVE_INJURIES_FILE = os.path.join(DATA_DIR, "active_injuries.json")
CFBD_ANALYTICS_FILE = os.path.join(DATA_DIR, "cfbd_analytics.json")
CFBD_STARTERS_FILE = os.path.join(DATA_DIR, "cfbd_starters.json")

# Mapping of Covers team slugs to normalized CFBD team names
COVERS_SLUG_MAP = {
    "air-force-falcons": "Air Force",
    "akron-zips": "Akron",
    "alabama-crimson-tide": "Alabama",
    "appalachian-state-mountaineers": "Appalachian State",
    "arizona-wildcats": "Arizona",
    "arizona-state-sun-devils": "Arizona State",
    "arkansas-razorbacks": "Arkansas",
    "arkansas-state-red-wolves": "Arkansas State",
    "army-black-knights": "Army",
    "auburn-tigers": "Auburn",
    "ball-state-cardinals": "Ball State",
    "baylor-bears": "Baylor",
    "boise-state-broncos": "Boise State",
    "boston-college-eagles": "Boston College",
    "bowling-green-falcons": "Bowling Green",
    "buffalo-bulls": "Buffalo",
    "byu-cougars": "BYU",
    "california-golden-bears": "California",
    "central-michigan-chippewas": "Central Michigan",
    "charlotte-49ers": "Charlotte",
    "cincinnati-bearcats": "Cincinnati",
    "clemson-tigers": "Clemson",
    "coastal-carolina-chanticleers": "Coastal Carolina",
    "colorado-buffaloes": "Colorado",
    "colorado-state-rams": "Colorado State",
    "connecticut-huskies": "Connecticut",
    "delaware-fightin-blue-hens": "Delaware",
    "duke-blue-devils": "Duke",
    "east-carolina-pirates": "East Carolina",
    "eastern-michigan-eagles": "Eastern Michigan",
    "fiu-panthers": "FIU",
    "florida-gators": "Florida",
    "florida-atlantic-owls": "Florida Atlantic",
    "florida-international-panthers": "FIU",
    "florida-state-seminoles": "Florida State",
    "fresno-state-bulldogs": "Fresno State",
    "georgia-bulldogs": "Georgia",
    "georgia-southern-eagles": "Georgia Southern",
    "georgia-state-panthers": "Georgia State",
    "georgia-tech-yellow-jackets": "Georgia Tech",
    "hawaii-rainbow-warriors": "Hawai'i",
    "hawai-i-rainbow-warriors": "Hawai'i",
    "houston-cougars": "Houston",
    "illinois-fighting-illini": "Illinois",
    "indiana-hoosiers": "Indiana",
    "iowa-hawkeyes": "Iowa",
    "iowa-state-cyclones": "Iowa State",
    "jacksonville-state-gamecocks": "Jacksonville State",
    "james-madison-dukes": "James Madison",
    "kansas-jayhawks": "Kansas",
    "kansas-state-wildcats": "Kansas State",
    "kennesaw-state-owls": "Kennesaw State",
    "kent-state-golden-flashes": "Kent State",
    "kentucky-wildcats": "Kentucky",
    "liberty-flames": "Liberty",
    "louisiana-ragin-cajuns": "Louisiana",
    "louisiana-monroe-warhawks": "UL Monroe",
    "louisiana-tech-bulldogs": "Louisiana Tech",
    "louisville-cardinals": "Louisville",
    "lsu-tigers": "LSU",
    "marshall-thundering-herd": "Marshall",
    "maryland-terrapins": "Maryland",
    "massachusetts-minutemen": "Massachusetts",
    "memphis-tigers": "Memphis",
    "miami-fl-hurricanes": "Miami",
    "miami-oh-redhawks": "Miami (OH)",
    "michigan-wolverines": "Michigan",
    "michigan-state-spartans": "Michigan State",
    "middle-tennessee-blue-raiders": "Middle Tennessee",
    "minnesota-golden-gophers": "Minnesota",
    "mississippi-rebels": "Ole Miss",
    "mississippi-state-bulldogs": "Mississippi State",
    "missouri-tigers": "Missouri",
    "missouri-state-bears": "Missouri State",
    "navy-midshipmen": "Navy",
    "nc-state-wolfpack": "NC State",
    "nebraska-cornhuskers": "Nebraska",
    "nevada-wolf-pack": "Nevada",
    "new-mexico-lobos": "New Mexico",
    "new-mexico-state-aggies": "New Mexico State",
    "north-carolina-tar-heels": "North Carolina",
    "north-carolina-state-wolfpack": "NC State",
    "north-dakota-state-bison": "North Dakota State",
    "north-texas-mean-green": "North Texas",
    "northern-illinois-huskies": "Northern Illinois",
    "northwestern-wildcats": "Northwestern",
    "notre-dame-fighting-irish": "Notre Dame",
    "ohio-bobcats": "Ohio",
    "ohio-state-buckeyes": "Ohio State",
    "oklahoma-sooners": "Oklahoma",
    "oklahoma-state-cowboys": "Oklahoma State",
    "old-dominion-monarchs": "Old Dominion",
    "oregon-ducks": "Oregon",
    "oregon-state-beavers": "Oregon State",
    "penn-state-nittany-lions": "Penn State",
    "pittsburgh-panthers": "Pittsburgh",
    "purdue-boilermakers": "Purdue",
    "rice-owls": "Rice",
    "rutgers-scarlet-knights": "Rutgers",
    "sacramento-state-hornets": "Sacramento State",
    "sam-houston-bearkats": "Sam Houston",
    "san-diego-state-aztecs": "San Diego State",
    "san-jose-state-spartans": "San José State",
    "smu-mustangs": "SMU",
    "south-alabama-jaguars": "South Alabama",
    "south-carolina-gamecocks": "South Carolina",
    "south-florida-bulls": "South Florida",
    "southern-miss-golden-eagles": "Southern Mississippi",
    "stanford-cardinal": "Stanford",
    "syracuse-orange": "Syracuse",
    "tcu-horned-frogs": "TCU",
    "temple-owls": "Temple",
    "tennessee-volunteers": "Tennessee",
    "texas-longhorns": "Texas",
    "texas-am-aggies": "Texas A&M",
    "texas-a-m-aggies": "Texas A&M",
    "texas-state-bobcats": "Texas State",
    "texas-tech-red-raiders": "Texas Tech",
    "toledo-rockets": "Toledo",
    "troy-trojans": "Troy",
    "tulane-green-wave": "Tulane",
    "tulsa-golden-hurricane": "Tulsa",
    "uab-blazers": "UAB",
    "ucf-knights": "UCF",
    "ucla-bruins": "UCLA",
    "uconn-huskies": "Connecticut",
    "umass-minutemen": "Massachusetts",
    "unlv-rebels": "UNLV",
    "usc-trojans": "USC",
    "utah-utes": "Utah",
    "utah-state-aggies": "Utah State",
    "utep-miners": "UTEP",
    "utsa-roadrunners": "UTSA",
    "vanderbilt-commodores": "Vanderbilt",
    "virginia-cavaliers": "Virginia",
    "virginia-tech-hokies": "Virginia Tech",
    "wake-forest-demon-deacons": "Wake Forest",
    "washington-huskies": "Washington",
    "washington-state-cougars": "Washington State",
    "west-virginia-mountaineers": "West Virginia",
    "western-kentucky-hilltoppers": "Western Kentucky",
    "western-michigan-broncos": "Western Michigan",
    "wisconsin-badgers": "Wisconsin",
    "wyoming-cowboys": "Wyoming",
}

POWER_4_CONFERENCES = {"SEC", "Big Ten", "Big 12", "ACC"}


def _load_team_analytics() -> dict[str, dict]:
    """Load cached team analytics to determine team strength and conference."""
    if os.path.exists(CFBD_ANALYTICS_FILE):
        try:
            with open(CFBD_ANALYTICS_FILE, "r") as f:
                data = json.load(f)
                return {t["name"]: t for t in data if "name" in t}
        except Exception:
            pass
    return {}


def _load_team_starters() -> dict[str, dict]:
    """Fetch or load CFBD passing and skill leaders to identify true starting QBs and key skill players."""
    if os.path.exists(CFBD_STARTERS_FILE):
        try:
            with open(CFBD_STARTERS_FILE, "r") as f:
                data = json.load(f)
                if data and isinstance(data, dict):
                    return data
        except Exception:
            pass

    import app
    client = app.httpx.Client(timeout=25)
    starters = {}
    try:
        r = client.get(f"{app.CFBD_BASE}/stats/player/season?year={app.CFBD_YEAR}&category=passing", headers=app.CFBD_HEADERS)
        if r.status_code != 200 or not r.json():
            r = client.get(f"{app.CFBD_BASE}/stats/player/season?year={app.CFBD_YEAR_FALLBACK}&category=passing", headers=app.CFBD_HEADERS)
        if r.status_code == 200:
            for row in r.json():
                if row.get("statType") == "ATT":
                    team = row.get("team")
                    player = row.get("player")
                    att = float(row.get("stat", 0))
                    team_entry = starters.setdefault(team, {"qb": None})
                    if not team_entry["qb"] or att > team_entry["qb"]["att"]:
                        team_entry["qb"] = {
                            "player": player,
                            "att": att,
                            "last_name": player.split()[-1].lower() if player else ""
                        }
            if starters:
                with open(CFBD_STARTERS_FILE, "w") as f:
                    json.dump(starters, f, indent=2)
                print(f"[starters] Cached 2026 starting QBs for {len(starters)} teams")
    except Exception as e:
        print(f"[starters] fetch error: {e}")
    return starters


def calculate_player_deduction(
    team_name: str,
    player_name: str,
    pos: str,
    status: str,
    team_info: dict,
    starters_map: dict = None
) -> tuple[float, str]:
    """
    Calculate point deduction for an injured player.
    Returns (points_deducted_as_negative, tier_label).
    Jeff Tracy directive: 'A major star qb in college out is massive I'd say 10 points at star level'.
    CRITICAL CHECK: Must verify player is the actual STARTING QB.
    Backup QBs receive 0.0 points (no penalty).
    Tiers:
      - Star QB (Top 25 composite >= 80 or SP+ >= 18): -10.0 pts
      - P4 Starter QB: -7.0 pts
      - G5 Starter QB: -4.5 pts
      - Backup QB: 0.0 pts
      - Key skill position (RB/WR): -1.5 pts
    Status multipliers:
      - Out / Doubtful / Surgery / IR / Suspended: 1.0 (100%)
      - Questionable: 0.5 (50%)
    """
    pos = pos.upper().strip()
    stat_lower = status.lower()

    # Determine status severity multiplier
    if any(s in stat_lower for s in ["out", "doubtful", "surgery", "suspended", "season"]):
        mult = 1.0
    elif any(s in stat_lower for s in ["questionable", "gtd", "decision"]):
        mult = 0.5
    else:
        mult = 0.0

    if mult == 0.0:
        return 0.0, "active"

    conf = team_info.get("conf", "")
    comp = team_info.get("composite", 50.0)
    sp = team_info.get("sp_plus", 0.0)

    if pos == "QB":
        # Check against true team starting QB
        starters = starters_map or {}
        starter_entry = starters.get(team_name, {})
        if isinstance(starter_entry, dict) and "qb" in starter_entry and isinstance(starter_entry["qb"], dict):
            starter_info = starter_entry["qb"]
        elif isinstance(starter_entry, dict):
            starter_info = starter_entry
        else:
            starter_info = {}
        starter_last = starter_info.get("last_name", "").lower() if starter_info else ""
        player_last = player_name.split()[-1].lower() if player_name else ""

        # If team has a known starter and this player is NOT the starter, deduction is 0.0!
        if starter_last and player_last != starter_last:
            return 0.0, "backup_qb"

        # Star QB criteria: high composite or high SP+ on elite programs
        if comp >= 80.0 or sp >= 18.0 or team_name in ["Texas", "Georgia", "Ohio State", "Alabama", "Miami", "Notre Dame", "Oregon", "LSU", "USC", "Tennessee", "Penn State"]:
            base_pts = 10.0
            tier = "star_qb"
        elif conf in POWER_4_CONFERENCES or team_name == "Notre Dame":
            base_pts = 7.0
            tier = "p4_starter_qb"
        else:
            base_pts = 4.5
            tier = "g5_starter_qb"
        return round(-base_pts * mult, 1), tier

    elif pos in ("RB", "WR", "TE"):
        base_pts = 1.5
        tier = "key_skill"
        return round(-base_pts * mult, 1), tier

    elif pos in ("DE", "DT", "EDGE", "LB", "CB", "S"):
        base_pts = 1.0
        tier = "defensive_key"
        return round(-base_pts * mult, 1), tier

    return 0.0, "bench"


def scrape_injuries() -> dict:
    """Scrape Covers CFB injuries and return structured active injuries by team."""
    url = "https://www.covers.com/sport/football/ncaaf/injuries"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    with httpx.Client(timeout=15, headers=headers) as client:
        resp = client.get(url)
        resp.raise_for_status()
        html = resp.text

    blocks = re.findall(
        r'<div class="covers-CoversMatchups-teamName">.*?<a[^>]*href="[^"]*/teams/main/([^"]+)"[^>]*>(.*?)</a>.*?id="injuryCollapse([A-Z0-9]+)"[^>]*>(.*?)</table>',
        html,
        re.DOTALL
    )

    team_analytics = _load_team_analytics()
    starters_map = _load_team_starters()
    existing_store = {}
    if os.path.exists(ACTIVE_INJURIES_FILE):
        try:
            with open(ACTIVE_INJURIES_FILE, "r") as f:
                existing_store = json.load(f).get("teams", {})
        except Exception:
            pass

    teams_out = {}
    total_injuries = 0
    total_qb_deductions = 0

    for slug, raw_name, code, tbl in blocks:
        cfbd_team = COVERS_SLUG_MAP.get(slug)
        if not cfbd_team:
            continue

        team_info = team_analytics.get(cfbd_team, {})
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", tbl, re.DOTALL)
        team_injuries = []
        net_deduction = 0.0

        for row in rows:
            tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
            if len(tds) >= 3:
                player = " ".join(re.sub(r"<[^>]+>", " ", tds[0]).split())
                pos = re.sub(r"<[^>]+>", "", tds[1]).strip().upper()
                status = " ".join(re.sub(r"<[^>]+>", " ", tds[2]).split())

                if player in ("Player", "") or not pos:
                    continue

                pts, tier = calculate_player_deduction(cfbd_team, player, pos, status, team_info, starters_map)

                # Cap non-QB deductions at -3.0 max per team so rotational depth isn't crushed
                if pos != "QB" and pts < 0:
                    current_non_qb = sum(i["deduction"] for i in team_injuries if i["pos"] != "QB")
                    if current_non_qb + pts < -3.0:
                        pts = max(0.0, -3.0 - current_non_qb)
                        if pts == 0.0:
                            continue

                # Only include players that actually generate a deduction (or QBs for visibility if deduction < 0)
                if pts < 0.0:
                    team_injuries.append({
                        "player": player,
                        "pos": pos,
                        "status": status,
                        "tier": tier,
                        "deduction": pts,
                        "updated": datetime.now().strftime("%Y-%m-%d"),
                    })
                    net_deduction += pts
                    total_injuries += 1
                    if pos == "QB" and pts <= -4.5:
                        total_qb_deductions += 1

        # Check if manual overrides exist in previous file
        prev_manual = existing_store.get(cfbd_team, {}).get("manual_overrides", [])
        if prev_manual:
            for mo in prev_manual:
                team_injuries.append(mo)
                net_deduction += mo.get("deduction", 0.0)

        if team_injuries:
            teams_out[cfbd_team] = {
                "team": cfbd_team,
                "net_injury_points": round(net_deduction, 1),
                "injuries": team_injuries,
                "manual_overrides": prev_manual,
                "updated_at": datetime.now().isoformat(),
            }

    payload = {
        "updated": datetime.now().isoformat(),
        "total_teams_with_injuries": len(teams_out),
        "total_tracked_injuries": total_injuries,
        "key_qb_injuries": total_qb_deductions,
        "teams": teams_out,
    }

    with open(ACTIVE_INJURIES_FILE, "w") as f:
        json.dump(payload, f, indent=2)

    print(f"[+] Scraped {len(teams_out)} teams with injuries ({total_injuries} players, {total_qb_deductions} key QBs)")
    return payload


if __name__ == "__main__":
    scrape_injuries()
