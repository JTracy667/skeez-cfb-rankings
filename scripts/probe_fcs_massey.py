"""Probe CFBD for the FCS / FCS-Coaches-Poll / Massey pieces the CEO directive lists
as 'remaining backfill'. Read-only, ~6 calls. Prints only summaries."""
import json, os, sys
sys.path.insert(0, r"C:\Users\jtracy\dev\cfb-power-rankings")
import cfbd_shared as cs

def show(label, data):
    if isinstance(data, list):
        print(f"{label}: {len(data)} rows")
        if data:
            print("   sample:", json.dumps(data[0])[:300])
    else:
        print(f"{label}: {type(data).__name__} -> {json.dumps(data)[:300]}")

for label, ep, kw in [
    ("/games 2025 FBS", "games", {"year": 2025}),
    ("/games 2025 division=fcs", "games", {"year": 2025, "division": "fcs"}),
    ("/stats/season 2025 division=fcs", "stats/season", {"year": 2025, "division": "fcs"}),
    ("/rankings 2025 wk1", "rankings", {"year": 2025, "week": 1, "seasonType": "regular"}),
    ("/ratings/massey 2025", "ratings/massey", {"year": 2025}),
]:
    try:
        d = cs.cfbd_get(ep, **kw)
        show(label, d)
        if label.endswith("wk1") and isinstance(d, list):
            for e in d[:1]:
                for p in (e.get("polls") or []):
                    print("   poll:", p.get("poll"), "ranks:", len(p.get("ranks") or []))
    except Exception as e:
        print(f"{label}: ERROR {type(e).__name__}: {e}")
