#!/usr/bin/env python3
"""selftest_rankings_version.py — QA finding: the daily guard must be VERSION-aware.

Defect: daily_rankings() guarded on the DATE alone, so once a partition existed
under the legacy literal "composite" it was never rewritten. A mid-day config
change could therefore never appear in the archive — defeating risk-register D3,
whose whole purpose is that a weight change creates a VISIBLE version break.

This must be fixed WITHOUT reintroducing the 26-rows-vs-25-teams bug the guard was
originally added for (two teams both ranked 25, two same-day writes unioned).

Asserts, against the REAL D1 table:
  1. a different model_version FORCES a rewrite (the finding)
  2. the SAME model_version SKIPS (the guard still works, no double-write)
  3. row count stays exactly 25 after every step (no union / duplicate ranks)
  4. only ONE model_version is present for the date at any time
  5. the table is left carrying the CURRENT composite_version()

Run:  D1_WRITE_ENABLED=1 REFRESH_INTERVAL_SECONDS=0 \
        python scripts/selftest_rankings_version.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

os.environ.setdefault("REFRESH_INTERVAL_SECONDS", "0")
os.environ.setdefault("D1_WRITE_ENABLED", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app            # noqa: E402
import d1_store       # noqa: E402
import d1_write_path  # noqa: E402

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")
FAILS: list[str] = []
N = 0


def check(label, ok, detail=""):
    global N
    N += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        FAILS.append(label)


def state():
    rows = d1_store.query(
        "SELECT model_version, COUNT(*) n, COUNT(DISTINCT rank) distinct_ranks "
        "FROM rankings_daily WHERE date = ? GROUP BY model_version", [TODAY])
    return rows or []


def show(label):
    s = state()
    print(f"    {label}: {s}")
    return s


def main() -> int:
    if not d1_write_path.enabled():
        print("D1 write path disabled — set D1_WRITE_ENABLED=1")
        return 1

    cur = app.composite_version()
    print(f"today={TODAY}  current model_version={cur}\n")

    print("0. baseline")
    show("before")

    print("\n1. a DIFFERENT version must force a rewrite (the QA finding)")
    n1 = d1_write_path.daily_rankings(lambda: app.get_rankings().teams,
                                      app.CFBD_YEAR, None, "cTESTVERSION")
    s = show("after write with cTESTVERSION")
    check("write happened (not skipped)", n1 > 0, f"rows={n1}")
    check("stored version is now the test version",
          any(r["model_version"] == "cTESTVERSION" for r in s))
    total = sum(int(r["n"]) for r in s)
    check("exactly 25 rows (no union)", total == 25, f"{total}")
    check("no duplicate ranks", all(int(r["distinct_ranks"]) == int(r["n"]) for r in s),
          str([(r["model_version"], r["n"], r["distinct_ranks"]) for r in s]))

    print("\n2. the SAME version must still SKIP (no double-write)")
    n2 = d1_write_path.daily_rankings(lambda: app.get_rankings().teams,
                                      app.CFBD_YEAR, None, "cTESTVERSION")
    s = show("after repeat write")
    check("repeat write skipped", n2 == 0, f"rows={n2}")
    total = sum(int(r["n"]) for r in s)
    check("still exactly 25 rows", total == 25, f"{total}")

    print("\n3. restore the REAL version (this is the normal archive write)")
    n3 = d1_write_path.daily_rankings(lambda: app.get_rankings().teams,
                                      app.CFBD_YEAR, None, cur)
    s = show("after real write")
    check("rewrote back to the real version", n3 > 0, f"rows={n3}")
    check("ONLY the real version present", len(s) == 1 and s[0]["model_version"] == cur,
          str([r["model_version"] for r in s]))
    check("version starts with 'c' (not the legacy literal)",
          all(str(r["model_version"]).startswith("c") for r in s),
          str([r["model_version"] for r in s]))
    total = sum(int(r["n"]) for r in s)
    check("exactly 25 rows", total == 25, f"{total}")

    print(f"\n{N - len(FAILS)}/{N} PASS")
    if FAILS:
        print("FAILED: " + "; ".join(FAILS))
        return 1
    print("rankings_daily guard is version-aware and duplicate-free — VERIFIED")
    return 0


if __name__ == "__main__":
    sys.exit(main())