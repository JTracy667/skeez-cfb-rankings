#!/usr/bin/env python3
"""selftest_predictions.py — prove model_predictions can never hold hindsight.

Risk register D2: predictions must be written BEFORE kickoff. A post-hoc row
would silently poison every backtest built on this table while looking perfectly
healthy, so the guard lives at the write boundary.

Checks:
  1. a game whose kickoff has PASSED is refused (0 rows written)
  2. a game whose kickoff is in the FUTURE is written
  3. created_at < kickoff for every written row (no hindsight by construction)
  4. re-writing the same game is idempotent (no duplicate rows)
  5. a row with no kickoff at all is refused (can't prove it's pre-game)

Run:  D1_WRITE_ENABLED=1 python scripts/selftest_predictions.py
Exit 0 = all pass. Cleans up its own rows.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import d1_store        # noqa: E402
import d1_write_path   # noqa: E402

VERSION = "selftest"
ID_PAST, ID_FUTURE, ID_NOKICK = 999000001, 999000002, 999000003
FAILS: list[str] = []
N = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global N
    N += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        FAILS.append(label)


def rows() -> list[dict]:
    return d1_store.query(
        "SELECT game_id, predicted_margin_home, predicted_total, win_prob_home, created_at"
        " FROM model_predictions WHERE model_version = ?", [VERSION])


def cleanup() -> None:
    try:
        d1_store.query_full("DELETE FROM model_predictions WHERE model_version = ?", [VERSION])
    except Exception as e:  # noqa: BLE001
        print(f"  (cleanup warning: {e})")


def main() -> int:
    now = datetime.now(timezone.utc)
    past = (now - timedelta(days=1)).isoformat()
    future = (now + timedelta(days=2)).isoformat()
    print(f"\nnow={now.isoformat()}\n  past kickoff  = {past}\n  future kickoff= {future}")

    cleanup()

    print("\n1. write one past-kickoff and one future-kickoff game")
    n = d1_write_path.snapshot_predictions([
        {"game_id": ID_PAST, "date": past, "predicted_margin_home": -7.5,
         "predicted_total": 51.0, "win_prob_home": 0.61},
        {"game_id": ID_FUTURE, "date": future, "predicted_margin_home": 3.2,
         "predicted_total": 55.5, "win_prob_home": 0.54},
    ], model_version=VERSION)
    got = rows()
    ids = [r["game_id"] for r in got]
    check("past-kickoff game REFUSED", ID_PAST not in ids, f"ids written={ids}")
    check("future-kickoff game written", ID_FUTURE in ids, f"ids written={ids}")
    check("exactly one row written", len(got) == 1, f"{len(got)} rows, api said {n}")

    print("\n2. no hindsight: created_at must precede kickoff")
    if got:
        created = datetime.fromisoformat(str(got[0]["created_at"]).replace("Z", "+00:00"))
        kick = datetime.fromisoformat(future)
        check("created_at < kickoff", created < kick, f"{created.isoformat()} < {kick.isoformat()}")

    print("\n3. idempotent re-write")
    d1_write_path.snapshot_predictions([
        {"game_id": ID_FUTURE, "date": future, "predicted_margin_home": 3.2,
         "predicted_total": 55.5, "win_prob_home": 0.54},
    ], model_version=VERSION)
    again = rows()
    check("still one row for the game", len(again) == 1, f"{len(again)} rows")

    print("\n4. a game with NO kickoff is refused (cannot prove it is pre-game)")
    before = len(rows())
    d1_write_path.snapshot_predictions([
        {"game_id": ID_NOKICK, "predicted_margin_home": 1.0,
         "predicted_total": 50.0, "win_prob_home": 0.5},
    ], model_version=VERSION)
    after = rows()
    check("no-kickoff game refused", len(after) == before and ID_NOKICK not in
          [r["game_id"] for r in after], f"{len(after)} rows")

    print("\n5. real values round-trip (what the model said is what got stored)")
    if again:
        r = again[0]
        check("margin stored", abs(float(r["predicted_margin_home"]) - 3.2) < 1e-9,
              str(r["predicted_margin_home"]))
        check("total stored", abs(float(r["predicted_total"]) - 55.5) < 1e-9,
              str(r["predicted_total"]))
        check("win prob stored", abs(float(r["win_prob_home"]) - 0.54) < 1e-9,
              str(r["win_prob_home"]))

    cleanup()
    print(f"\n{N - len(FAILS)}/{N} PASS")
    if FAILS:
        print("FAILED: " + "; ".join(FAILS))
        return 1
    print("model_predictions: pre-kickoff only — VERIFIED (no hindsight path exists)")
    return 0


if __name__ == "__main__":
    sys.exit(main())