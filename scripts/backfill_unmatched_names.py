#!/usr/bin/env python3
"""backfill_unmatched_names.py — close the name->id gaps the 2021-2026 backfill
silently skipped.

The run ended with `UNMATCHED team names (skipped): ['Albany', 'Southeastern
Louisiana', 'UTRGV']`. All three ARE in `teams` — CFBD's ratings/stats endpoints just
emit a DIFFERENT string than /teams' `school`:

    ratings say             /teams school
    -------------           ----------------------
    Albany                  UAlbany            (team_id 399)
    Southeastern Louisiana  SE Louisiana       (team_id 2545)
    UTRGV                   UT Rio Grande Valley (team_id 292)

This script re-fetches the rating/season-stat endpoints for every season, keeps only
the rows for those names, and writes them as stat_observations — so the archive holds
the same data the endpoints do, with no silent loss. Idempotent (unique-index upsert).

Run:  CF_D1_TOKEN=... python -u scripts/backfill_unmatched_names.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cfbd_shared  # noqa: E402
import d1_store  # noqa: E402

SEASONS = [2021, 2022, 2023, 2024, 2025, 2026]
ENDPOINTS = [("ratings/elo", "elo", "elo"),
             ("ratings/sp", "sp_plus", "rating"),
             ("ratings/sp", "sp_plus_rk", "ranking"),
             ("ratings/fpi", "fpi", "fpi"),
             ("talent", "talent_rating", "talent"),
             ("recruiting/teams", "recruiting_class_rank", "rank")]
NAMES = ["Albany", "Southeastern Louisiana", "UTRGV"]
CALLS = 0


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    global CALLS
    aliases = cfbd_shared.team_aliases()
    ids = {n: aliases.get(n) for n in NAMES}
    missing = [n for n, i in ids.items() if not i]
    print(f"alias resolution: {ids}" + (f"  !! unresolved: {missing}" if missing else ""),
          flush=True)
    if missing:
        return 1

    total, now = 0, _ts()
    for season in SEASONS:
        obs = []
        for ep, key, field in ENDPOINTS:
            CALLS += 1
            for t in cfbd_shared.cfbd_get(ep, year=season) or []:
                if t.get("team") not in NAMES or t.get(field) is None:
                    continue
                obs.append({"subject_type": "team", "subject_id": ids[t["team"]],
                            "season": season, "week": 0, "stat_key": key,
                            "value": t.get(field), "source": "cfbd", "recorded_at": now})
        CALLS += 1
        for t in cfbd_shared.cfbd_get("stats/season", year=season) or []:
            if t.get("team") not in NAMES or t.get("statValue") is None:
                continue
            try:
                v = float(t["statValue"])
            except (TypeError, ValueError):
                continue
            obs.append({"subject_type": "team", "subject_id": ids[t["team"]],
                        "season": season, "week": 0, "stat_key": t.get("statName"),
                        "value": v, "source": "cfbd", "recorded_at": now})
        if obs:
            n = d1_store.upsert_stat_observations(obs)
            total += n
            print(f"  {season}: {len(obs)} obs for {NAMES} -> {n} confirmed writes",
                  flush=True)
        else:
            print(f"  {season}: no rows for {NAMES}", flush=True)
    print(f"DONE unmatched-name repair: rows_written={total} cfbd_calls={CALLS} "
          f"d1_today={d1_store.ledger_written()}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())