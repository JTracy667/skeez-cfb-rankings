#!/usr/bin/env python3
"""
Fix team name normalization to use full mascot names from The Odds API.
Bookmaker sends 'North Carolina Tar Heels' -> should normalize to 'North Carolina'
Bookmaker sends 'North Dakota State Bison' -> should normalize to 'North Dakota State'
"""

path = 'C:/Users/JeffTracy/Desktop/cfb-power-rankings/app.py'

with open(path, 'r') as f:
    content = f.read()

# Build the full mascot-to-internal mapping
mascot_map = {
    # FBS / Power 5 / Group of 5
    "Alabama Crimson Tide": "Alabama",
    "Arkansas Razorbacks": "Arkansas",
    "Auburn Tigers": "Auburn",
    "BYU Cougars": "BYU",
    "Baylor Bears": "Baylor",
    "Boise State Broncos": "Boise State",
    "California Golden Bears": "California",
    "Cincinnati Bearcats": "Cincinnati",
    "Clemson Tigers": "Clemson",
    "Colorado Buffaloes": "Colorado",
    "Duke Blue Devils": "Duke",
    "East Carolina Pirates": "East Carolina",
    "Florida Atlantic Owls": "Florida Atlantic",
    "Florida Gators": "Florida",
    "Florida State Seminoles": "Florida State",
    "Fresno State Bulldogs": "Fresno State",
    "Georgia Bulldogs": "Georgia",
    "Georgia Tech Yellow Jackets": "Georgia Tech",
    "Houston Cougars": "Houston",
    "Illinois Fighting Illini": "Illinois",
    "Indiana Hoosiers": "Indiana",
    "Iowa Hawkeyes": "Iowa",
    "Iowa State Cyclones": "Iowa State",
    "Kansas Jayhawks": "Kansas",
    "Kansas State Wildcats": "Kansas State",
    "Kentucky Wildcats": "Kentucky",
    "Louisville Cardinals": "Louisville",
    "LSU Tigers": "LSU",
    "Marshall Thundering Herd": "Marshall",
    "Memphis Tigers": "Memphis",
    "Miami Hurricanes": "Miami",
    "Michigan State Spartans": "Michigan State",
    "Michigan Wolverines": "Michigan",
    "Minnesota Golden Gophers": "Minnesota",
    "Mississippi State Bulldogs": "Mississippi State",
    "Missouri Tigers": "Missouri",
    "NC State Wolfpack": "NC State",
    "Nebraska Cornhuskers": "Nebraska",
    "Nevada Wolf Pack": "Nevada",
    "New Mexico Lobos": "New Mexico",
    "North Carolina Tar Heels": "North Carolina",
    "North Texas Mean Green": "North Texas",
    "Northwestern Wildcats": "Northwestern",
    "Notre Dame Fighting Irish": "Notre Dame",
    "Ohio State Buckeyes": "Ohio State",
    "Oklahoma Sooners": "Oklahoma",
    "Oklahoma State Cowboys": "Oklahoma State",
    "Ole Miss Rebels": "Ole Miss",
    "Oregon Ducks": "Oregon",
    "Oregon State Beavers": "Oregon State",
    "Penn State Nittany Lions": "Penn State",
    "Pittsburgh Panthers": "Pittsburgh",
    "Purdue Boilermakers": "Purdue",
    "Rice Owls": "Rice",
    "Rutgers Scarlet Knights": "Rutgers",
    "Sam Houston State Bearkats": "Sam Houston",
    "San Diego State Aztecs": "San Diego State",
    "San Jose State Spartans": "San Jose State",
    "SMU Mustangs": "SMU",
    "South Carolina Gamecocks": "South Carolina",
    "South Florida Bulls": "South Florida",
    "Stanford Cardinal": "Stanford",
    "Syracuse Orange": "Syracuse",
    "TCU Horned Frogs": "TCU",
    "Temple Owls": "Temple",
    "Tennessee Volunteers": "Tennessee",
    "Texas A&M Aggies": "Texas A&M",
    "Texas Longhorns": "Texas",
    "Texas Tech Red Raiders": "Texas Tech",
    "Tulane Green Wave": "Tulane",
    "UAB Blazers": "UAB",
    "UCF Knights": "UCF",
    "UCLA Bruins": "UCLA",
    "UTSA Roadrunners": "UTSA",
    "Utah Utes": "Utah",
    "Utah State Aggies": "Utah State",
    "Vanderbilt Commodores": "Vanderbilt",
    "Virginia Cavaliers": "Virginia",
    "Virginia Tech Hokies": "Virginia Tech",
    "Wake Forest Demon Deacons": "Wake Forest",
    "Washington Huskies": "Washington",
    "Washington State Cougars": "Washington State",
    "West Virginia Mountaineers": "West Virginia",
    "Wisconsin Badgers": "Wisconsin",
    # Conference USA, MAC, Sun Belt, MWC
    "Air Force Falcons": "Air Force",
    "Akron Zips": "Akron",
    "Appalachian State Mountaineers": "Appalachian State",
    "Arizona State Sun Devils": "Arizona State",
    "Arizona Wildcats": "Arizona",
    "Austin Peay Governors": "Austin Peay",
    "Ball State Cardinals": "Ball State",
    "Bethune-Cookman Wildcats": "Bethune-Cookman",
    "Bowling Green Falcons": "Bowling Green",
    "Buffalo Bulls": "Buffalo",
    "Central Michigan Chippewas": "Central Michigan",
    "Charlotte 49ers": "Charlotte",
    "Coastal Carolina Chanticleers": "Coastal Carolina",
    "Colorado State Rams": "Colorado State",
    "Delaware Blue Hens": "Delaware",
    "Duquesne Dukes": "Duquesne",
    "Eastern Michigan Eagles": "Eastern Michigan",
    "Florida International Panthers": "Florida International",
    "Fordham Rams": "Fordham",
    "Georgia Southern Eagles": "Georgia Southern",
    "Georgia State Panthers": "Georgia State",
    "Hawaii Rainbow Warriors": "Hawaii",
    "Idaho Vandals": "Idaho",
    "Indiana State Sycamores": "Indiana State",
    "Jacksonville State Gamecocks": "Jacksonville State",
    "James Madison Dukes": "James Madison",
    "Kennesaw State Owls": "Kennesaw State",
    "Kent State Golden Flashes": "Kent State",
    "Liberty Flames": "Liberty",
    "Louisiana Ragin Cajuns": "Louisiana",
    "Louisiana Tech Bulldogs": "Louisiana Tech",
    "Maine Black Bears": "Maine",
    "Middle Tennessee Blue Raiders": "Middle Tennessee",
    "Mississippi Valley State Delta Devils": "Mississippi Valley State",
    "Murray State Racers": "Murray State",
    "Navy Midshipmen": "Navy",
    "Nicholls State Colonels": "Nicholls",
    "New Hampshire Wildcats": "New Hampshire",
    "New Mexico State Aggies": "New Mexico State",
    "North Alabama Lions": "North Alabama",
    "North Carolina A&T Aggies": "North Carolina A&T",
    "North Dakota State Bison": "North Dakota State",
    "Northern Arizona Lumberjacks": "Northern Arizona",
    "Northern Illinois Huskies": "Northern Illinois",
    "Ohio Bobcats": "Ohio",
    "Old Dominion Monarchs": "Old Dominion",
    "Portland State Vikings": "Portland State",
    "Rhode Island Rams": "Rhode Island",
    "Sacramento State Hornets": "Sacramento State",
    "San Diego State Aztecs": "San Diego State",
    "South Alabama Jaguars": "South Alabama",
    "South Dakota State Jackrabbs": "South Dakota State",
    "Southern Mississippi Golden Eagles": "Southern Miss",
    "Toledo Rockets": "Toledo",
    "Troy Trojans": "Troy",
    "Tulsa Golden Hurricane": "Tulsa",
    "UMass Minutemen": "UMass",
    "UNLV Rebels": "UNLV",
    "UTEP Miners": "UTEP",
    "UT Rio Grande Valley Vaqueros": "UTRGV",
    "Western Kentucky Hilltoppers": "Western Kentucky",
    "Western Michigan Broncos": "Western Michigan",
    "Youngstown St Penguins": "Youngstown State",
    # FCS / others
    "Abilene Christian Wildcats": "Abilene Christian",
    "Albany": "Albany",
    "Alcorn State Braves": "Alcorn State",
    "Arkansas Pine Bluff Golden Lions": "Arkansas-Pine Bluff",
    "Army Black Knights": "Army",
    "Bryant Bulldogs": "Bryant",
    "Charleston Southern Buccaneers": "Charleston Southern",
    "Citadel Bulldogs": "Citadel",
    "Eastern Illinois Panthers": "Eastern Illinois",
    "Eastern Kentucky Colonels": "Eastern Kentucky",
    "Furman Paladins": "Furman",
    "Houston Baptist Huskies": "Houston Baptist",
    "Idaho State Bengals": "Idaho State",
    "Lafayette Leopards": "Lafayette",
    "Lamar Cardinals": "Lamar",
    "LIU Sharks": "LIU",
    "Mercyhurst Lakers": "Mercyhurst",
    "Merrimack Warriors": "Merrimack",
    "Miami (OH) RedHawks": "Miami (OH)",
    "Missouri State Bears": "Missouri State",
    "Morgan State Bears": "Morgan State",
    "Norfolk State Spartans": "Norfolk State",
    "Northwestern State Demons": "Northwestern State",
    "Southeast Missouri State Redhawks": "SE Missouri State",
    "Southeastern Louisiana Lions": "Southeastern Louisiana",
    "Tarleton State Texans": "Tarleton State",
    "Towson Tigers": "Towson",
    "UL Monroe Warhawks": "UL Monroe",
    "UConn Huskies": "UConn",
    "Utah Tech Trailblazers": "Utah Tech",
    "VMI Keydets": "VMI",
    "Wyoming Cowboys": "Wyoming",
}

# Build the Python list literal
map_lines = []
for mascot, school in mascot_map.items():
    map_lines.append(f'        ("{mascot}", "{school}"),')

map_literal = '[\n' + '\n'.join(map_lines) + '\n    ]'

# Replace the old _name_map in the normalize function
old_map_marker = '# Sorted list of (pattern, school) - longer patterns checked FIRST\n    _name_map = ['
new_map_block = f'''# Sorted list of (pattern, school) - longer patterns checked FIRST
    _name_map = {map_literal}
    for pattern, school in _name_map:
        if pattern.lower() in name_lower:
            return school'''

# Find and replace the entire normalize function
import re

old_func_pattern = r'(def _normalize_team_name\(name: str\) -> str:.*?)(\n\ndef |\Z)'
match = re.search(old_func_pattern, content, re.DOTALL)

if match:
    # Build new function
    new_func = f'''def _normalize_team_name(name: str) -> str:
    """Normalize bookmaker team names to our internal team names.
    Bookmakers use 'Location Mascot' format; we use 'Location' or 'School'.
    IMPORTANT: Check longer names first to avoid 'North Carolina' -> 'North'."""
    name_lower = name.lower()
    # Sorted list of (pattern, school) - longer patterns checked FIRST
    _name_map = {map_literal}
    for pattern, school in _name_map:
        if pattern.lower() in name_lower:
            return school
    # Fallback: first word
    parts = name.split()
    if parts:
        return parts[0]
    return name

'''
    content = content[:match.start(1)] + new_func + content[match.end(1):]
    with open(path, 'w') as f:
        f.write(content)
    print(f"Mascot normalization applied! {len(mascot_map)} mappings.")
else:
    print("ERROR: Could not find _normalize_team_name function")
