"""Build full FBS team database from ESPN scoreboard + hardcoded conference list."""
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "data" / "fbs_teams.json"

# Hardcoded FBS conferences (2024 realignment)
# Maps ESPN location/abbreviation -> conference
# Using exact location names as ESPN returns them
CONF_MAP = {
    # SEC (16)
    "Alabama": "SEC", "Arkansas": "SEC", "Auburn": "SEC", "Florida": "SEC",
    "Georgia": "SEC", "Kentucky": "SEC", "LSU": "SEC", "Mississippi State": "SEC",
    "Missouri": "SEC", "Oklahoma": "SEC", "Ole Miss": "SEC",
    "South Carolina": "SEC", "Tennessee": "SEC", "Texas": "SEC",
    "Texas A&M": "SEC", "Vanderbilt": "SEC",
    # Big Ten (18)
    "Illinois": "Big Ten", "Indiana": "Big Ten", "Iowa": "Big Ten",
    "Maryland": "Big Ten", "Michigan": "Big Ten", "Michigan State": "Big Ten",
    "Minnesota": "Big Ten", "Nebraska": "Big Ten", "Northwestern": "Big Ten",
    "Ohio State": "Big Ten", "Oregon": "Big Ten", "Penn State": "Big Ten",
    "Purdue": "Big Ten", "Rutgers": "Big Ten", "USC": "Big Ten",
    "Washington": "Big Ten", "Wisconsin": "Big Ten",
    # ACC (15)
    "Boston College": "ACC", "Cal": "ACC", "Clemson": "ACC", "Duke": "ACC",
    "Florida State": "ACC", "Georgia Tech": "ACC", "Louisville": "ACC",
    "Miami": "ACC", "NC State": "ACC", "Notre Dame": "ACC",
    "North Carolina": "ACC", "Pittsburgh": "ACC", "Syracuse": "ACC",
    "Virginia": "ACC", "Virginia Tech": "ACC", "Wake Forest": "ACC",
    # Big 12 (14)
    "Baylor": "Big 12", "BYU": "Big 12", "Cincinnati": "Big 12",
    "Houston": "Big 12", "Kansas": "Big 12", "Kansas State": "Big 12",
    "Oklahoma State": "Big 12", "SMU": "Big 12", "TCU": "Big 12",
    "Temple": "Big 12", "Texas Tech": "Big 12", "UCF": "Big 12",
    "West Virginia": "Big 12",
    # Pac-12 (8)
    "Arizona": "Pac-12", "Arizona State": "Pac-12", "Colorado": "Pac-12",
    "Oregon State": "Pac-12", "Stanford": "Pac-12", "Utah": "Pac-12",
    "UCLA": "Big Ten", "Washington State": "Pac-12",
    # American Athletic (12)
    "Charlotte": "American Athletic", "East Carolina": "American Athletic",
    "FIU": "American Athletic", "Florida Atlantic": "American Athletic",
    "Memphis": "American Athletic", "North Texas": "American Athletic",
    "Rice": "American Athletic", "Tulane": "American Athletic",
    "Tulsa": "American Athletic", "UTSA": "American Athletic",
    # Conference USA (14)
    "Appalachian State": "Conference USA", "Jacksonville State": "Conference USA",
    "Kennesaw State": "Conference USA", "Liberty": "Conference USA",
    "Louisiana Tech": "Conference USA", "Middle Tennessee": "Conference USA",
    "New Mexico State": "Conference USA", "Sam Houston": "Conference USA",
    "Southern Miss": "Conference USA", "Texas State": "Conference USA",
    "Western Kentucky": "Conference USA",
    # MAC (12)
    "Akron": "MAC", "Ball State": "MAC", "Bowling Green": "MAC",
    "Buffalo": "MAC", "Central Michigan": "MAC", "Eastern Michigan": "MAC",
    "Kent State": "MAC", "MiamiOH": "MAC", "Northern Illinois": "MAC",
    "Ohio": "MAC", "Toledo": "MAC", "Western Michigan": "MAC",
    # Mountain West (12)
    "Air Force": "Mountain West", "Boise State": "Mountain West",
    "Colorado State": "Mountain West", "Fresno State": "Mountain West",
    "Hawaii": "Mountain West", "Navy": "Mountain West",
    "San Diego State": "Mountain West", "San Jose State": "Mountain West",
    "UNLV": "Mountain West", "Utah State": "Mountain West",
    "Wyoming": "Mountain West",
    # Sun Belt (14)
    "Arkansas State": "Sun Belt", "Coastal Carolina": "Sun Belt",
    "Georgia State": "Sun Belt", "Georgia Southern": "Sun Belt",
    "James Madison": "Sun Belt", "Louisiana": "Sun Belt",
    "Louisiana-Monroe": "Sun Belt", "Marshall": "Sun Belt",
    "Old Dominion": "Sun Belt", "Troy": "Sun Belt",
}

# Also map abbreviations
ABBR_MAP = {
    # SEC
    "ALA": "SEC", "ARK": "SEC", "AUB": "SEC", "FLA": "SEC",
    "UGA": "SEC", "UK": "SEC", "LSU": "SEC", "MSST": "SEC",
    "MIZ": "SEC", "OU": "SEC", "MISS": "SEC",
    "SC": "SEC", "TENN": "SEC", "TEX": "SEC",
    "TA&M": "SEC", "VAN": "SEC",
    # Big Ten
    "ILL": "Big Ten", "IND": "Big Ten", "IOWA": "Big Ten",
    "MD": "Big Ten", "MICH": "Big Ten", "MSU": "Big Ten",
    "MINN": "Big Ten", "NEB": "Big Ten", "NW": "Big Ten",
    "OSU": "Big Ten", "OR": "Big Ten", "PSU": "Big Ten",
    "PUR": "Big Ten", "RU": "Big Ten", "USC": "Big Ten",
    "WASH": "Big Ten", "WIS": "Big Ten",
    # ACC
    "BC": "ACC", "CAL": "ACC", "CLEM": "ACC", "DUKE": "ACC",
    "FSU": "ACC", "GT": "ACC", "LOU": "ACC",
    "MIA": "ACC", "NCST": "ACC", "ND": "ACC",
    "UNC": "ACC", "PITT": "ACC", "SYR": "ACC", "UVA": "ACC",
    "VT": "ACC", "WF": "ACC",
    # Big 12
    "BAY": "Big 12", "BYU": "Big 12", "CIN": "Big 12",
    "HOU": "Big 12", "KU": "Big 12", "KSU": "Big 12",
    "OKST": "Big 12", "SMU": "Big 12", "TCU": "Big 12",
    "TEMP": "Big 12", "TTU": "Big 12", "UCF": "Big 12", "WV": "Big 12",
    # Pac-12
    "AZ": "Pac-12", "AZST": "Pac-12", "COLO": "Pac-12",
    "ORST": "Pac-12", "STAN": "Pac-12", "UTAH": "Pac-12",
    "UCLA": "Big Ten", "WSU": "Pac-12",
    # American
    "UNCC": "American Athletic", "ECU": "American Athletic",
    "FIU": "American Athletic", "FAU": "American Athletic",
    "MEM": "American Athletic", "UNT": "American Athletic",
    "RICE": "American Athletic", "TULN": "American Athletic",
    "TUL": "American Athletic", "UTSA": "American Athletic",
    # C-USA
    "APP": "Conference USA", "JSU": "Conference USA",
    "KSU2": "Conference USA", "LIB": "Conference USA",
    "LAT": "Conference USA", "MT": "Conference USA",
    "NMSU": "Conference USA", "SHSU": "Conference USA",
    "USM": "Conference USA", "TXST": "Conference USA",
    "WKU": "Conference USA",
    # MAC
    "AKR": "MAC", "BSU": "MAC", "BG": "MAC",
    "BUF": "MAC", "CMU": "MAC", "EMU": "MAC",
    "KST": "MAC", "MHOH": "MAC", "NIU": "MAC",
    "OHIO": "MAC", "TOL": "MAC", "WMU": "MAC",
    # MW
    "FA": "Mountain West", "BS": "Mountain West",
    "CSU": "Mountain West", "FRES": "Mountain West",
    "HAW": "Mountain West", "NAVY": "Mountain West",
    "SDSU": "Mountain West", "SJSU": "Mountain West",
    "UNLV": "Mountain West", "USU": "Mountain West",
    "WYO": "Mountain West",
    # Sun Belt
    "ARST": "Sun Belt", "CCU": "Sun Belt",
    "GST": "Sun Belt", "GSU": "Sun Belt",
    "JMU": "Sun Belt", "LA": "Sun Belt",
    "ULM": "Sun Belt", "MARSH": "Sun Belt",
    "ODU": "Sun Belt", "TROY": "Sun Belt",
}

def resolve_conference(team_info: dict) -> str:
    """Resolve conference for a team. Only exact matches on location/abbreviation."""
    loc = team_info.get("location", "")
    abbr = team_info.get("abbreviation", "")
    
    # Exact match on location
    if loc in CONF_MAP:
        return CONF_MAP[loc]
    # Exact match on abbreviation
    if abbr in ABBR_MAP:
        return ABBR_MAP[abbr]
    
    return "FBS"

def main():
    # Collect teams from all scoreboard weeks
    teams = {}
    files = [
        BASE / "data" / "espn_scoreboard.json",
        BASE / "data" / "espn_week2.json",
        BASE / "data" / "espn_week3.json",
        BASE / "data" / "espn_week4.json",
    ]
    for fn in files:
        if not fn.exists():
            print(f"  Skipping {fn.name} (not found)")
            continue
        with open(fn) as f:
            data = json.load(f)
        for ev in data.get("events", []):
            for c in ev.get("competitions", [{}])[0].get("competitors", []):
                t = c.get("team", {})
                name = t.get("displayName", "")
                if name and name not in teams:
                    teams[name] = {
                        "name": name,
                        "abbreviation": t.get("abbreviation", ""),
                        "location": t.get("location", ""),
                        "nickname": t.get("nickname", ""),
                        "conference": resolve_conference(t),
                        "color": t.get("color", ""),
                        "logo": t.get("logo", ""),
                    }
    
    OUT.parent.mkdir(parents=True, exist_ok=True)
    out_list = sorted(teams.values(), key=lambda x: (x["conference"], x["name"]))
    with open(OUT, "w") as f:
        json.dump(out_list, f, indent=2)
    print(f"Wrote {len(out_list)} teams to {OUT}")
    from collections import Counter
    confs = Counter(t["conference"] for t in out_list)
    for conf, count in confs.most_common():
        print(f"  {conf}: {count}")

if __name__ == "__main__":
    main()