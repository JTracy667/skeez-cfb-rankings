#!/usr/bin/env python3
"""d1_season_receipt.py — per-season row counts in D1 (Phase 3 receipts).

Usage: python scripts/d1_season_receipt.py
"""
from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))
import d1_store  # noqa: E402
from d1_receipt import _bootstrap_token  # noqa: E402


def main() -> int:
    _bootstrap_token()
    print(f"{'season':8s} {'games':>8s} {'stat_obs':>9s} {'lines':>7s}")
    for season in range(2021, 2027):
        g = d1_store.query("SELECT COUNT(*) AS n FROM games WHERE season=?", [season])[0]["n"]
        s = d1_store.query("SELECT COUNT(*) AS n FROM stat_observations WHERE season=?",
                           [season])[0]["n"]
        l = d1_store.query(
            "SELECT COUNT(*) AS n FROM closing_lines WHERE game_id IN "
            "(SELECT game_id FROM games WHERE season=?)", [season])[0]["n"]
        print(f"{season:<8d} {g:>8d} {s:>9d} {l:>7d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
