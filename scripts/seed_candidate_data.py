"""Seed the local candidate's data directory from the repo, and repair clobbered caches.

The served candidate reads CFB_DATA_DIR (a scratch copy), not the repo's data/. Two problems
showed up there:

  1. `cfbd_season_games.json` had been overwritten with `{"ts":..., "games": []}` (39 bytes) by the
     app's own unconditional write -- fixed in app.py, but the damage is already on disk.
  2. Even undamaged, an offline process refuses a disk copy older than 4x the TTL (2h), so a
     candidate seeded from a day-old repo file would serve an EMPTY schedule.

So: re-copy any JSON that is present in the repo but empty/missing in the scratch copy, and stamp
the season-games fixture's `ts` at seed time so the offline process will actually serve it. The
game DATA is copied verbatim -- only the cache-freshness stamp is local, and it is local-only
(this file is never run against the repo's data/ or production).

    python scripts/seed_candidate_data.py [target-data-dir]
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time

REPO_DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
DEFAULT_TARGET = os.path.join(
    os.environ.get("LOCALAPPDATA", ""), "hermes", "profiles", "cto", "cache", "scratch",
    "candidate_serve", "data")
# Files whose absence empties a page. Kept explicit rather than copying all of data/.
REQUIRED = ("cfbd_season_games.json", "cfbd_analytics.json", "cfbd_starters.json",
            "cfbd_player_ppa.json", "active_injuries.json", "cfbd_logos.json")


def _games(path: str) -> int:
    try:
        with open(path, encoding="utf-8") as f:
            return len(json.load(f).get("games") or [])
    except Exception:
        return -1


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TARGET
    os.makedirs(target, exist_ok=True)
    print(f"seeding {target}")
    for name in REQUIRED:
        src = os.path.join(REPO_DATA, name)
        dst = os.path.join(target, name)
        if not os.path.exists(src):
            print(f"  {name}: not in the repo, skipped")
            continue
        # Re-copy when the scratch copy is missing or is a clobbered stub (empty list/dict).
        needs = not os.path.exists(dst)
        if not needs:
            try:
                with open(dst, encoding="utf-8") as f:
                    payload = json.load(f)
                if isinstance(payload, list):
                    needs = len(payload) == 0
                elif isinstance(payload, dict):
                    inner = payload.get("games")
                    needs = inner is not None and len(inner) == 0
            except Exception:
                needs = True
        if needs:
            shutil.copy2(src, dst)
            print(f"  {name}: copied from the repo ({os.path.getsize(dst)} bytes)")
        else:
            print(f"  {name}: present, left alone")

    # Freshness stamp: the offline process will not serve a copy older than 2h.
    gpath = os.path.join(target, "cfbd_season_games.json")
    if os.path.exists(gpath):
        try:
            with open(gpath, encoding="utf-8") as f:
                payload = json.load(f)
            n = len(payload.get("games") or [])
            payload["ts"] = time.time()
            with open(gpath, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            print(f"  cfbd_season_games.json: {n} games, ts stamped at seed time (local fixture)")
        except Exception as e:  # noqa: BLE001
            print(f"  !! could not stamp the games fixture: {e}")
            return 1
    print(f"games visible to the candidate: {_games(gpath)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())