#!/usr/bin/env python3
"""selftest_book_of_record.py — proves DECISION A actually takes effect.

Decision A: a line from one of Jeff's books (betonlineag / betmgm / williamhill_us) is the
SOURCE OF RECORD and must OUTRANK the cross-book /best-line consensus. The consensus only
fills a market his book has not posted.

Drives app._build_odds_map with synthetic odds_data, so this tests the real merge logic
rather than a reimplementation of it. Run: python scripts/selftest_book_of_record.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: E402

FAIL = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r} want {want!r}")
    if not ok:
        FAIL.append(label)


def best_line(spread_pt, total_pt, book="draftkings", title="DraftKings"):
    return {"spread": {"point": spread_pt, "side": "Auburn Tigers", "book": book,
                       "book_title": title},
            "total": {"point": total_pt, "book": book, "book_title": title}}


def game(books, bl=None):
    """books: list of (key, title, spread_point, total_point)"""
    return {"home_team": "Auburn Tigers", "away_team": "Missouri Tigers",
            "best_line": bl or {},
            "bookmakers": [
                {"key": k, "title": t, "markets": [
                    {"key": "spreads", "outcomes": [
                        {"name": "Auburn Tigers", "point": sp},
                        {"name": "Missouri Tigers", "point": -sp if sp is not None else None}]},
                    {"key": "totals", "outcomes": [{"name": "Over", "point": tot}]},
                ]} for k, t, sp, tot in books]}


KEY = (app._normalize_team_name("Auburn Tigers"), app._normalize_team_name("Missouri Tigers"))

print("CASE 1 — betonlineag has a line, consensus disagrees (the core of decision A)")
m = app._build_odds_map([game([("betonlineag", "BetOnline.ag", -7.0, 51.5)],
                              best_line(-7.5, 51.0))])[KEY]
check("spread is OUR book's line, not the consensus", m["spread"], -7.0)
check("total is OUR book's line", m["total"], 51.5)
check("spread_kind", m["spread_kind"], "book_of_record")
check("book attributed to us", m["book"], "betonlineag")

print("\nCASE 2 — our book absent: consensus must win (behaviour must not regress)")
m = app._build_odds_map([game([("draftkings", "DraftKings", -7.5, 51.0)],
                              best_line(-7.5, 51.0))])[KEY]
check("spread from consensus", m["spread"], -7.5)
check("spread_kind", m["spread_kind"], "best_line")
check("book = consensus book", m["book"], "draftkings")

print("\nCASE 3 — our book has SPREAD only: per-market fallback")
g = game([("betonlineag", "BetOnline.ag", -7.0, None)], best_line(-7.5, 51.0))
m = app._build_odds_map([g])[KEY]
check("spread still ours", m["spread"], -7.0)
check("spread_kind ours", m["spread_kind"], "book_of_record")
check("total falls back to consensus", m["total"], 51.0)
check("total_kind consensus", m["total_kind"], "best_line")

print("\nCASE 4 — record chain order is betonlineag > williamhill_us > betmgm (Jeff, 2026-09-24)")
m = app._build_odds_map([game([("williamhill_us", "Caesars", -6.5, 50.5),
                               ("betmgm", "BetMGM", -7.0, 51.0)],
                              best_line(-7.5, 51.0))])[KEY]
check("williamhill_us outranks betmgm", m["book"], "williamhill_us")
check("line is williamhill_us's", m["spread"], -6.5)
check("kind is book_of_record", m["spread_kind"], "book_of_record")

print("\nCASE 4b — betonlineag missing: William Hill is used for everything it misses")
m = app._build_odds_map([game([("betonlineag", "BetOnline.ag", None, None),
                               ("williamhill_us", "Caesars", -6.5, 50.5),
                               ("betmgm", "BetMGM", -7.0, 51.0)],
                              best_line(-7.5, 51.0))])[KEY]
check("spread falls to williamhill_us", m["spread"], -6.5)
check("total falls to williamhill_us", m["total"], 50.5)
check("book attributed to williamhill_us", m["book"], "williamhill_us")

print("\nCASE 4c — per-market walk: betonlineag spread only, William Hill total")
m = app._build_odds_map([game([("betonlineag", "BetOnline.ag", -7.0, None),
                               ("williamhill_us", "Caesars", -6.0, 50.5)],
                              best_line(-7.5, 51.0))])[KEY]
check("spread stays betonlineag's", m["spread"], -7.0)
check("total is William Hill's, not the consensus", m["total"], 50.5)
check("total_kind", m["total_kind"], "book_of_record")
check("total book attributed", m["total_book_title"], "Caesars")

print("\nCASE 5 — non-Jeff book (draftkings) must NOT become book-of-record")
m = app._build_odds_map([game([("draftkings", "DraftKings", -9.0, 51.0)],
                              best_line(-7.5, 51.0))])[KEY]
check("consensus still wins over a non-Jeff book", m["spread"], -7.5)
check("kind stays best_line", m["spread_kind"], "best_line")

print()
if FAIL:
    print(f"FAILED {len(FAIL)}: {FAIL}")
    raise SystemExit(1)
print("ALL CASES PASS — decision A is live in the merge logic")