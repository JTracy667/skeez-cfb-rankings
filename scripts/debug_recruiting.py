"""Debug recruiting page HTML structure."""
import re

with open("/c/tmp/rec_test.html", "r") as f:
    html = f.read()

# Find rec-card blocks
cards = re.findall(r'<div class="rec-card">(.*?)</div>\s*<div class="rec-rank">#(\d+)</div>\s*<div class="rec-points">([\d.]+)\s*pts</div>', html, re.DOTALL)
print(f"Pattern 1 (card then rank/points): {len(cards)} matches")

# Try: rank and points are INSIDE rec-card
cards2 = re.findall(r'<div class="rec-card">(.*?)</div>', html, re.DOTALL)
print(f"Pattern 2 (all rec-card blocks): {len(cards2)} matches")

if cards2:
    print("\nFirst card content (first 800 chars):")
    print(cards2[0][:800])
    print("\n---\n")
    print("Second card content (first 800 chars):")
    print(cards2[1][:800])

# Count team names
team_names = re.findall(r'<a class="rec-team-name"[^>]*href="[^"]*team=([^">]+)', html)
print(f"\nTeam name links found: {len(team_names)}")
print(f"First 10: {team_names[:10]}")

# Count ranks
ranks = re.findall(r'<div class="rec-rank">#(\d+)</div>', html)
print(f"\nRank divs found: {len(ranks)}")
print(f"First 10: {ranks[:10]}")

# Count points
points = re.findall(r'<div class="rec-points">([\d.]+)\s*pts</div>', html)
print(f"\nPoints divs found: {len(points)}")
print(f"First 10: {points[:10]}")
