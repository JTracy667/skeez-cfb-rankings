#!/usr/bin/env python3
"""odds_history_coverage.py — what the Phase 1 harvest can actually support.

WHY THIS SCRIPT EXISTS
----------------------
A first pass measured coverage by asking "does this game appear in the OPEN file and the CLOSE
file?" and came back with 1,241 / 285 games. That metric is WRONG and understates the harvest by
an order of magnitude.

Snapshots are weekly, not per-game, so a given game may not be posted yet at the "open" timestamp
(it simply is not on the board), while a different game is. Pooling every snapshot for a game and
taking the EARLIEST and LATEST observation before its own kickoff is the correct derivation, and it
is what the V4 arms must use.

Rule: for each game, consider only snapshots whose timestamp precedes its commence_time.
      open = earliest such observation, close = latest such observation.
"""
from __future__ import annotations

import collections
import glob
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HIST = ROOT / "data" / "odds_history"


def dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load() -> dict:
    """game_id -> {commence, home, away, obs:[(ts, {book: {spread, total}})]}"""
    games = {}
    for path in sorted(glob.glob(str(HIST / "*.json"))):
        week = json.loads(Path(path).read_text(encoding="utf-8"))
        for snap in week.get("snapshots", []):
            ts = dt(snap["snapshot_ts"])
            for ev in snap.get("events", []):
                g = games.setdefault(ev["id"], {
                    "commence": ev["commence_time"], "home": ev["home_team"],
                    "away": ev["away_team"], "obs": []})
                books = {}
                for bk, v in ev.get("bookmakers", {}).items():
                    spread = None
                    for o in v.get("spreads", []):
                        if o.get("point") is not None:
                            spread = o["point"]
                            break
                    total = None
                    for o in v.get("totals", []):
                        if o.get("point") is not None:
                            total = o["point"]
                            break
                    books[bk] = {"spread": spread, "total": total}
                g["obs"].append((ts, books))
    return games


def report(games: dict, book: str | None = None) -> dict:
    n_any = n_multi = n_book = 0
    for g in games.values():
        ct = dt(g["commence"])
        pre = sorted([o for o in g["obs"] if o[0] < ct], key=lambda x: x[0])
        if not pre:
            continue
        n_any += 1
        if len(pre) >= 2:
            n_multi += 1
            if book:
                pts = [o[1].get(book, {}).get("spread") for o in pre]
                if pts[0] is not None and pts[-1] is not None:
                    n_book += 1
    return {"observed": len(games), "with_pre_kickoff": n_any,
            "open_close_derivable": n_multi, f"with_{book}_both": n_book if book else None}


if __name__ == "__main__":
    games = load()
    print("games observed                       :", len(games))
    for bk in (None, "betonlineag", "betmgm", "williamhill_us"):
        r = report(games, bk)
        print(f"  open/close derivable {str(bk):<14}: {r['open_close_derivable']}"
              + (f"   (book present in both: {r['with_' + str(bk) + '_both']})" if bk else ""))