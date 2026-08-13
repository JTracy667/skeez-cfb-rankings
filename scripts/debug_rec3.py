"""Debug recruiting card parsing."""
import re
import httpx

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
with httpx.Client(timeout=15) as client:
    resp = client.get("https://www.puntandrally.com/recruiting.php?whichyear=2026", headers=HEADERS, follow_redirects=True)
    html = resp.text

# The issue: <div class="rec-card"> contains nested </div> tags
# Strategy: split on rec-card markers instead of regex
parts = html.split('<div class="rec-card">')
print(f"Split into {len(parts)} parts")

teams_found = 0
for i, part in enumerate(parts[1:]):  # skip before first card
    # Find team name
    name_match = re.search(r'<a class="rec-team-name"[^>]*href="[^"]*team=([^">]+)', part)
    rank_match = re.search(r'<div class="rec-rank">#(\d+)</div>', part)
    pts_match = re.search(r'<div class="rec-points">([\d.]+)\s*pts</div>', part)
    
    if name_match:
        name = name_match.group(1).replace("%20", " ").replace("%26", "&")
        rank = rank_match.group(1) if rank_match else "?"
        pts = pts_match.group(1) if pts_match else "?"
        teams_found += 1
        if teams_found <= 5:
            print(f"  Rank #{rank}: {name} — {pts} pts")

print(f"\nTotal teams found: {teams_found}")
