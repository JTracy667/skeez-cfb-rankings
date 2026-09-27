"""Phase 1 acceptance receipt: does the analytics record gain the new fields
without losing any, and does it stay valid JSON?

Runs fetch_live_analytics() with CFBD served from the on-disk RAW fixtures, so it
spends NO quota, then compares the produced record schema against the live
data/cfbd_analytics.json that is currently on disk.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
from tests.test_advanced_stats_ingest import _FixtureCFBD, _advanced, FIXTURES  # noqa: E402

fake = _FixtureCFBD()
app._cfbd_get = fake
_stubs = {
    "_cfbd_teams": lambda: {t: {"nickname": "", "conference": "MWC"} for t in _advanced()},
    "_cfbd_recruiting": lambda: {},
    "_cfbd_talent": lambda: {},
    "_cfbd_srs": lambda: {},
    "_cfbd_elo": lambda: {},
    "_cfbd_season_stats": lambda: {},
    "_cfbd_ppa": lambda: {},
    "_cfbd_drives_for_teams": lambda names: {},
    "_cfbd_returning": lambda: {},
    "_cfbd_roster_experience": lambda: {},
    "_cfbd_records": lambda year=app.CFBD_YEAR: {},
}
for name, fn in _stubs.items():
    setattr(app, name, fn)

teams = app.fetch_live_analytics()
new_keys = set(teams[0].keys())

live_path = ROOT / "data" / "cfbd_analytics.json"
live = json.loads(live_path.read_text(encoding="utf-8"))
old_keys = set(live[0].keys())

added = sorted(new_keys - old_keys)
lost = sorted(old_keys - new_keys)

print(f"live file: {live_path.name}  teams={len(live)}  fields/record={len(old_keys)}")
print(f"rebuilt  : teams={len(teams)}  fields/record={len(new_keys)}")
print(f"cfbd endpoints used: {sorted(set(fake.calls))}")
print(f"\nADDED ({len(added)}):")
for k in added:
    print(f"  + {k}")
print(f"\nLOST ({len(lost)}): {lost if lost else 'none -- additive only'}")

blob = json.dumps(teams)
assert json.loads(blob) == teams, "JSON round-trip mismatch"
print(f"\nJSON round-trip: OK ({len(blob)} bytes)")

missing_adv = [f for f in app.ADV_MATCHUP_FIELDS if f not in new_keys]
print(f"ADV_MATCHUP_FIELDS present: {len(app.ADV_MATCHUP_FIELDS) - len(missing_adv)}"
      f"/{len(app.ADV_MATCHUP_FIELDS)}  missing={missing_adv}")
print(f"RESULT: {'PASS' if not lost and not missing_adv else 'FAIL'}")
