"""Verify the FBS scoping of per-event best-line calls, using the ARCHIVED feed.

Zero API cost on purpose: the real PropLine response is already in D1 (archived daily by
_maybe_snapshot_propline), so the filter can be tested against genuine data instead of a
hand-made fixture -- and without spending the metered quota the filter exists to protect.

What it asserts:
  1. no event with an FBS side is ever dropped (that would lose a real game's line), and
  2. the events dropped are non-FBS on both sides.

Usage:  python scripts/test_fbs_line_scope.py
"""
import base64
import gzip
import json
import os
import re
import sys
import datetime as dt
from pathlib import Path

sys.path.insert(0, ".")

_ENV = Path(__file__).resolve().parent.parent / ".env"
if _ENV.exists():
    for _line in _ENV.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

import app  # noqa: E402
import cfbd_shared  # noqa: E402
import d1_store  # noqa: E402


def truth_is_fbs(ev) -> bool:
    """Independent classification via the teams table (not the filter's own helper)."""
    names = {}
    for t in (cfbd_shared.teams_by_name() or {}).values():
        names[app._norm_team_name(t.get("school"))] = t.get("classification")
    for side in ("home_team", "away_team"):
        if names.get(app._norm_team_name(ev.get(side))) == "fbs":
            return True
    return False


rows = d1_store.query("SELECT endpoint, payload_gz FROM raw_payloads "
                      "WHERE endpoint LIKE 'propline/odds_full/%' ORDER BY endpoint DESC LIMIT 1")
if not rows:
    print("no archived PropLine payload found in raw_payloads")
    raise SystemExit(1)

ep = rows[0]["endpoint"]
data = json.loads(gzip.decompress(base64.b64decode(rows[0]["payload_gz"])).decode("utf-8"))
events = data if isinstance(data, list) else data.get("events", data)
print(f"archived feed: {ep}  events={len(events)}")

kept = app._fbs_due_only(list(events))
kept_ids = {e.get("id") for e in kept}
dropped = [e for e in events if e.get("id") not in kept_ids]

fbs_all = [e for e in events if truth_is_fbs(e)]
fbs_dropped = [e for e in fbs_all if e.get("id") not in kept_ids]
nonfbs_dropped_ok = [e for e in dropped if not truth_is_fbs(e)]

now = dt.datetime.now(dt.timezone.utc)
def near(ev, hrs=48):
    try:
        t = dt.datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        return now <= t <= now + dt.timedelta(hours=hrs)
    except Exception:
        return False

near_all = [e for e in events if near(e)]
near_kept = [e for e in near_all if e.get("id") in kept_ids]

print(f"\n  FBS-involved events in feed : {len(fbs_all)}")
print(f"  dropped total               : {len(dropped)}")
print(f"  dropped that were FBS       : {len(fbs_dropped)}   <-- must be 0")
print(f"  dropped that were non-FBS   : {len(nonfbs_dropped_ok)}")
print(f"\n  in the 48h 'every cycle' tier: {len(near_all)} -> kept {len(near_kept)} "
      f"(saves {len(near_all) - len(near_kept)} calls/cycle, "
      f"{round(100 * (len(near_all) - len(near_kept)) / max(len(near_all), 1))}%)")

if fbs_dropped:
    print("\n  FAIL -- these FBS games would have lost their line data:")
    for e in fbs_dropped[:10]:
        print(f"    {e.get('away_team')} @ {e.get('home_team')}")
    raise SystemExit(1)
print("\n  PASS -- no FBS-involved event was dropped; every drop was non-FBS on both sides.")