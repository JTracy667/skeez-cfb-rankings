"""Does CFBD /games carry FCS games (no filter available)? And does /teams expose
the division field so we could tag them? Read-only, 2 calls."""
import json, sys
sys.path.insert(0, r"C:\Users\jtracy\dev\cfb-power-rankings")
import cfbd_shared as cs

teams = cs.cfbd_get("teams", year=2025) or []
div = {}
for t in teams:
    div[t.get("school")] = (t.get("classification") or t.get("division") or "?")
print("teams:", len(teams), "| classifications:",
      {c: sum(1 for v in div.values() if v == c) for c in set(div.values())})
fbs = {s for s, d in div.items() if d == "fbs"}
print("fbs schools:", len(fbs))

games = cs.cfbd_get("games", year=2025) or []
n_fcs = 0
seen = set()
for g in games:
    for side in ("homeTeam", "awayTeam"):
        n = g.get(side)
        if n and div.get(n, "?") not in ("fbs",):
            n_fcs += 1
            seen.add(n)
print(f"games: {len(games)} | non-FBS team-slots: {n_fcs} | distinct non-FBS schools: {len(seen)}")
print("sample non-FBS schools:", sorted(seen)[:12])
