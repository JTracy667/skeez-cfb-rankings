"""
Fetch live SP+ and FPI from PuntAndRally, recruiting from ON3.
Usage: python scripts/fetch_live_analytics.py
Output: JSON to stdout with all metrics for each team.
"""

import json
import re
import httpx

# ── Sources ──
PAR_URL = "https://www.puntandrally.com"
ON3_URL = "https://www.on3.com/rivals/rankings/industry-team/football/2026/"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
YEAR = 2026

def fetch_html(url):
    """Fetch a page and return HTML text."""
    with httpx.Client(timeout=15) as client:
        resp = client.get(url, headers=HEADERS, follow_redirects=True)
        resp.raise_for_status()
        return resp.text

def parse_spplus(html):
    """Parse SP+ overall/offense/defense from viewSandPratings.php"""
    teams = {}
    pattern = r"(\d+)\.\s*<a[^>]*>[^<]*</a>\s*([-\d\.]+)\s*<i>\(([\d\.]+)/([\d\.]+)\)</i>"
    for m in re.finditer(pattern, html):
        link_match = re.search(r"team=([^']+)'", m.group(0))
        if link_match:
            name = link_match.group(1)
            teams[name] = {
                "sp_plus": float(m.group(2)),
                "sp_offense": float(m.group(3)),
                "sp_defense": float(m.group(4)),
                "sp_rank": int(m.group(1)),
            }
    return teams

def parse_fpi(html):
    """Parse Power Ratings (FPI) from viewpowerratings.php"""
    teams = {}
    pattern = r"(\d+)\.\s*<a[^>]*team=([^']+)'[^>]*>[^<]*</a>\s*([-\d\.]+)\s*&nbsp;<span[^>]*>.*?</span>\s*-\s*&nbsp;<span[^>]*>(.*?)</span>"
    for m in re.finditer(pattern, html):
        name = m.group(2)
        prev_match = re.search(r"#(\d+)", m.group(4))
        teams[name] = {
            "fpi": float(m.group(3)),
            "fpi_rank": int(m.group(1)),
            "fpi_prev_rank": int(prev_match.group(1)) if prev_match else int(m.group(1)),
        }
    return teams

def parse_on3_recruiting(html):
    """Parse recruiting rankings from ON3 __NEXT_DATA__ embedded JSON."""
    teams = {}
    # Extract __NEXT_DATA__ JSON block
    next_data_match = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        html,
        re.DOTALL,
    )
    if not next_data_match:
        print("    [!] No __NEXT_DATA__ found in ON3 page")
        return teams

    try:
        data = json.loads(next_data_match.group(1))
    except json.JSONDecodeError as e:
        print(f"    [!] Failed to parse __NEXT_DATA__: {e}")
        return teams

    # Navigate to teamData.list
    try:
        team_list = data["props"]["pageProps"]["teamData"]["list"]
    except (KeyError, TypeError):
        print("    [!] Could not find teamData.list in __NEXT_DATA__")
        return teams

    for entry in team_list:
        org = entry.get("organization", {})
        name = org.get("name", "")
        if not name:
            continue
        teams[name] = {
            "recruiting_rank": entry.get("overallRank", 0),
            "recruiting_pts": round(entry.get("appliedAverageConsensusRating", entry.get("appliedAverageRating", 0)), 2),
            "recruiting_commits": entry.get("appliedCommits", 0),
            "recruiting_5star": entry.get("fiveStars", 0),
            "recruiting_4star": entry.get("fourStars", 0),
        }
    return teams

def build_analytics():
    """Fetch all sources and merge into analytics records."""
    print("[*] Fetching SP+ ratings...")
    sp_html = fetch_html(f"{PAR_URL}/viewSandPratings.php?whichyear={YEAR}")
    sp_data = parse_spplus(sp_html)
    print(f"    Got {len(sp_data)} teams from SP+")

    print("[*] Fetching Power Ratings (FPI)...")
    fpi_html = fetch_html(f"{PAR_URL}/viewpowerratings.php?whichyear={YEAR}")
    fpi_data = parse_fpi(fpi_html)
    print(f"    Got {len(fpi_data)} teams from FPI")

    print("[*] Fetching Recruiting rankings from ON3...")
    rec_html = fetch_html(ON3_URL)
    rec_data = parse_on3_recruiting(rec_html)
    print(f"    Got {len(rec_data)} teams from ON3 Recruiting")

    # Load FBS conference map
    try:
        with open("data/fbs_teams.json") as f:
            fbs_teams = json.load(f)
        conf_map = {}
        for t in fbs_teams:
            for key in [t.get("displayName"), t.get("location"), t.get("name")]:
                if key:
                    conf_map[key] = t.get("conference", "FBS")
    except Exception:
        conf_map = {}
        print(f"    [!] Could not load fbs_teams.json")

    # Merge all data sources
    all_names = set(list(sp_data.keys()) + list(fpi_data.keys()) + list(rec_data.keys()))
    analytics = []

    for name in sorted(all_names, key=lambda n: sp_data.get(n, {}).get("sp_rank", 999)):
        sp = sp_data.get(name, {})
        fpi = fpi_data.get(name, {})
        rec = rec_data.get(name, {})

        sp_plus = sp.get("sp_plus", 0)
        fpi_score = fpi.get("fpi", 0)
        fpi_win_prob = max(0, min(100, 50 + fpi_score * 1.5))
        cpi = max(0, min(100, 50 + sp_plus * 2.0))
        rec_rank = rec.get("recruiting_rank") or 50
        coach_win_pct = max(0.5, min(0.95, 0.85 - (rec_rank - 1) * 0.005))
        sp_off = sp.get("sp_offense", 30)
        sp_def = sp.get("sp_defense", 30)

        analytics.append({
            "rank": sp.get("sp_rank", 0),
            "name": name,
            "mascot": "",
            "conf": conf_map.get(name, "FBS"),
            "emoji": "🏈",
            "wins": 0,
            "losses": 0,
            "points": sp_plus,
            "sp_plus": round(sp_plus, 2),
            "sp_offense": round(sp_off, 1),
            "sp_defense": round(sp_def, 1),
            "fpi": round(fpi_score, 2),
            "fpi_rank": fpi.get("fpi_rank", 0),
            "fpi_win_prob": round(fpi_win_prob, 1),
            "cpi": round(cpi, 1),
            "recruiting_rank": rec_rank,
            "recruiting_pts": round(rec.get("recruiting_pts", 0), 1),
            "recruiting_commits": rec.get("recruiting_commits", 0),
            "recruiting_5star": rec.get("recruiting_5star", 0),
            "recruiting_4star": rec.get("recruiting_4star", 0),
            "coach_win_pct": round(coach_win_pct, 2),
            "off_ppg": round(max(14, min(55, 20 + sp_off * 0.4)), 1),
            "off_ypp": round(max(3.5, min(8.0, 4.5 + sp_off * 0.06)), 2),
            "off_3rd": round(max(0.25, min(0.65, 0.35 + sp_off * 0.008)), 2),
            "def_ppg": round(max(10, min(50, 30 - sp_def * 0.3)), 1),
            "def_ypp": round(max(2.5, min(7.0, 5.5 - sp_def * 0.05)), 2),
            "def_3rd": round(max(0.2, min(0.55, 0.45 - sp_def * 0.006)), 2),
            "turnover_margin": round(sp_plus * 0.1, 1),
            "movement": 0,
            "streak": "—",
        })

    print(f"\n[+] Total analytics entries: {len(analytics)}")
    print(f"[+] FBS teams with conferences: {len(conf_map)}")
    return analytics

if __name__ == "__main__":
    result = build_analytics()
    print(json.dumps(result, indent=2))
