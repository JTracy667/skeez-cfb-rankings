#!/usr/bin/env python3
"""Fetch Week 1 2026 CFB schedule from ESPN API and show matchups."""
import json, urllib.request

URL = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard?year=2026&week=1"

with urllib.request.urlopen(URL) as r:
    data = json.loads(r.read())

print(f"Season: {data.get('season',{}).get('displayName','?')}")
print(f"Week: {data.get('week',{}).get('number','?')}")
print(f"Total games: {len(data.get('events',[]))}")
print()

for i, e in enumerate(data['events'][:10]):
    comp = e.get('competitions', [{}])[0]
    home = away = None
    for c in comp.get('competitors', []):
        t = c.get('team', {}).get('name', '?')
        if c.get('homeAway') == 'home':
            home = t
        else:
            away = t
    print(f"{i+1:3d}. {away} @ {home}")

print(f"\n... and {len(data['events'])-10} more games")
