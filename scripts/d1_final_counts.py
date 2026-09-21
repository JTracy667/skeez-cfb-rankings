"""Final D1 row counts per table + per-season games/obs breakdown. Read-only."""
import json, os, sys
sys.path.insert(0, r"C:\Users\jtracy\dev\cfb-power-rankings")
os.environ.setdefault("CF_D1_TOKEN", "")
import d1_store

def q(sql, params=None):
    return d1_store.query(sql, params)

tables = ["teams", "games", "stat_observations", "closing_lines", "rankings_daily",
          "odds_snapshots", "model_predictions", "players"]
print("== ROW COUNTS PER TABLE ==")
for t in tables:
    try:
        n = q(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"]
        print(f"{t:20s} {n:>9,}")
    except Exception as e:
        print(f"{t:20s} ERR {str(e)[:80]}")

print("\n== games by season ==")
for r in q("SELECT season, COUNT(*) AS n FROM games GROUP BY season ORDER BY season"):
    print(f"  {r['season']}: {r['n']:,}")
print("  total games:", q("SELECT COUNT(*) AS n FROM games")[0]["n"])

print("\n== games by division (join teams.classification) ==")
for r in q("SELECT COALESCE(t.classification,'?') AS c, COUNT(*) AS n FROM games g "
           "LEFT JOIN teams t ON t.team_id=g.home_id GROUP BY c ORDER BY n DESC"):
    print(f"  {r['c']}: {r['n']:,}")

print("\n== stat_observations by season (top keys) ==")
for r in q("SELECT season, COUNT(*) AS n FROM stat_observations GROUP BY season ORDER BY season"):
    print(f"  {r['season']}: {r['n']:,}")
print("  fcs_rating rows:", q("SELECT COUNT(*) AS n FROM stat_observations WHERE stat_key='fcs_rating'")[0]["n"])
for r in q("SELECT stat_key, COUNT(*) AS n FROM stat_observations GROUP BY stat_key ORDER BY n DESC LIMIT 8"):
    print(f"  key {r['stat_key']}: {r['n']:,}")

print("\n== closing_lines by season (via games) ==")
for r in q("SELECT g.season AS s, COUNT(*) AS n FROM closing_lines c "
           "JOIN games g ON g.game_id=c.game_id GROUP BY s ORDER BY s"):
    print(f"  {r['s']}: {r['n']:,}")

print("\n== unmatched names present in teams? ==")
for nm in ["Albany", "Southeastern Louisiana", "UTRGV"]:
    rows = q("SELECT team_id,name,classification FROM teams WHERE name LIKE ?", [f"%{nm}%"])
    print(f"  {nm}: {rows}")