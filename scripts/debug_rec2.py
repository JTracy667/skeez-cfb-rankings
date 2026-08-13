"""Debug recruiting page HTML structure."""
import re
import httpx

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
with httpx.Client(timeout=15) as client:
    resp = client.get("https://www.puntandrally.com/recruiting.php?whichyear=2026", headers=HEADERS, follow_redirects=True)
    html = resp.text

print(f"Size: {len(html)} bytes")

cards = re.findall(r'<div class="rec-card">(.*?)</div>', html, re.DOTALL)
print(f"rec-card blocks: {len(cards)}")

team_names = re.findall(r'<a class="rec-team-name"[^>]*href="[^"]*team=([^">]+)', html)
print(f"Team name links: {len(team_names)}")
print(f"First 10 teams: {team_names[:10]}")

ranks = re.findall(r'<div class="rec-rank">#(\d+)</div>', html)
print(f"Rank divs: {len(ranks)}")
print(f"First 10 ranks: {ranks[:10]}")

points = re.findall(r'<div class="rec-points">([\d.]+)\s*pts</div>', html)
print(f"Points divs: {len(points)}")
print(f"First 10 points: {points[:10]}")

if cards:
    print("\nFirst card content:")
    print(cards[0][:600])
