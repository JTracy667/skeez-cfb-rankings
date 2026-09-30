#!/usr/bin/env python3
"""Publish the FIRST analytics publication record (Task 9 transition step).

WHY THIS EXISTS
The serving door (Task 4/9) serves only a pull named by a publication record, and that record
is written by the pull path in the new code. Production D1 has none yet, so between the deploy
and the first completed pull there is nothing published. The OLD behaviour cannot be used as
the bridge: "identity from D1 + numerics selected by MAX(recorded_at)" is precisely the defect
this work order removes, and serving identity with no numerics would make the composite impute
50 for every team.

So the first record is established by a VERIFIED completeness test, never by trusting
`MAX(recorded_at)` (QA ruling Q2: "Do not label an arbitrary MAX(recorded_at) historical set
complete just to bootstrap"). A complete analytics pull writes the whole metric set for the
whole universe at ONE stamp, so a stamp is accepted only when its row count, team count AND
distinct metric-key count are all above a floor. Nothing that cannot be verified is published.

This is a PRODUCTION WRITE. It requires explicit approval and a D1 token, and it is delivered
UNAPPLIED with the candidate (work order: no production write under this order).

USAGE
  CF_D1_TOKEN=... python scripts/publish_analytics_publication.py              # dry run
  CF_D1_TOKEN=... python scripts/publish_analytics_publication.py --apply      # publishes
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import d1_store  # noqa: E402
import d1_write_path  # noqa: E402

IDENTITY_KEY = "analytics_identity"


def candidates(season: int) -> list[dict]:
    """Every stamp for the season with its coverage, newest first."""
    rows = d1_store.query(
        "SELECT recorded_at AS stamp, COUNT(*) AS n, COUNT(DISTINCT subject_id) AS teams, "
        "COUNT(DISTINCT stat_key) AS keys, MAX(week) AS week "
        "FROM stat_observations WHERE season = ? AND subject_type = 'team' "
        "GROUP BY recorded_at ORDER BY recorded_at DESC", [int(season)])
    return rows or []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=2026)
    ap.add_argument("--min-teams", type=int, default=600,
                    help="a complete pull covers the whole analytics universe (~687)")
    ap.add_argument("--min-keys", type=int, default=40,
                    help="a complete pull writes the full metric set per team (~60)")
    ap.add_argument("--apply", action="store_true", help="PUBLISH (production write)")
    a = ap.parse_args()

    if not os.environ.get("CF_D1_TOKEN"):
        print("FATAL: no CF_D1_TOKEN (this talks to the real D1)", file=sys.stderr)
        return 2

    rows = candidates(a.season)
    if not rows:
        print(f"no team observations for season {a.season}; nothing to publish")
        return 1

    print(f"{a.season}: {len(rows)} stamp(s), newest first")
    for r in rows[:5]:
        ok = int(r["teams"]) >= a.min_teams and int(r["keys"]) >= a.min_keys
        print(f"  {r['stamp']}  rows={r['n']:>7} teams={r['teams']:>4} keys={r['keys']:>3} "
              f"wk={r['week']}  {'COMPLETE-LOOKING' if ok else 'partial (rejected)'}")

    pick = next((r for r in rows
                 if int(r["teams"]) >= a.min_teams and int(r["keys"]) >= a.min_keys), None)
    if pick is None:
        print("REFUSING: no stamp passes the completeness floor. A partial pull must never be "
              "published; wait for a complete pull (the anchors run Sun-Wed 21:00 PT, and the "
              "12h freshness guard pulls on the next visitor).")
        return 1

    stamp, week = str(pick["stamp"]), int(pick["week"])
    ident_blob = d1_store.get_app_state(IDENTITY_KEY) or ""
    import json  # noqa: PLC0415
    try:
        identity = json.loads(ident_blob) if ident_blob else {}
    except Exception:  # noqa: BLE001
        print("WARNING: existing identity blob is malformed; publishing WITHOUT identity")
        identity = {}

    print(f"\nchosen: stamp={stamp} wk={week} rows={pick['n']} teams={pick['teams']} "
          f"identity_teams={len(identity) if isinstance(identity, dict) else 0}")

    if not a.apply:
        print("\nDRY RUN - nothing written. Re-run with --apply (after approval) to publish.")
        return 0

    n = d1_write_path.publish_analytics_pull(a.season, week, stamp, int(pick["n"]),
                                            int(pick["teams"]), identity)
    readback = d1_write_path.analytics_publication(a.season)
    print(f"\npublished rows={n}")
    print(f"read-back: {json.dumps(readback)[:300]}")
    served = d1_write_path.load_team_analytics(a.season)
    print(f"serving door now returns {len(served)} teams "
          f"(expected {pick['teams']})")
    return 0 if readback and len(served) == int(pick["teams"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())