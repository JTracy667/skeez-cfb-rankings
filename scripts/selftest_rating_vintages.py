"""Selftest: rating-vintage plumbing (which season each rating input came from).

Why this exists
---------------
SRS was being served from the PREVIOUS season (CFBD has not published this season's
`/ratings/srs`) while carrying 12% of the composite weight, and nothing on the page said
so. The fix is only trustworthy if the vintage is reported accurately, so this test pins
the precedence rules and — most importantly — that an UNKNOWN vintage reports nothing at
all rather than guessing. A wrong label is worse than no label.

Run:  python scripts/selftest_rating_vintages.py     (exit 0 = all pass)
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402

PASS = 0
FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    ok = got == want
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}\n          got  {got!r}\n          want {want!r}")


def main():
    print("rating-vintage plumbing")

    real_base = app.BASE_DIR
    tmp = Path(tempfile.mkdtemp(prefix="vintage_selftest_"))
    (tmp / "data").mkdir(parents=True, exist_ok=True)
    vintage_file = tmp / "data" / "rating_vintages.json"

    saved_registry = dict(app.RATING_SOURCE_YEARS)
    try:
        # 1. The durable record is read when it exists.
        vintage_file.write_text(json.dumps({"srs": 2025, "as_of_utc": "x", "detail": "y"}))
        app.BASE_DIR = tmp
        app.RATING_SOURCE_YEARS.clear()
        check("reads the durable record", app._rating_vintages([]), {"srs": 2025})

        # 2. Non-integer metadata is not mistaken for a vintage.
        vintage_file.write_text(json.dumps({"srs": 2025, "as_of_utc": "x", "detail": "y"}))
        v = app._rating_vintages([])
        check("ignores non-integer metadata keys", sorted(v.keys()), ["srs"])

        # 3. Rows win over the file (the rows are what was actually served).
        rows = [{"name": "A", "srs_source_year": 2026}]
        check("served rows override the file", app._rating_vintages(rows), {"srs": 2026})

        # 4. A live fetch in this process is authoritative over both.
        app.RATING_SOURCE_YEARS["srs"] = 2027
        check("live fetch overrides everything", app._rating_vintages(rows), {"srs": 2027})
        app.RATING_SOURCE_YEARS.clear()

        # 5. THE IMPORTANT ONE: unknown => report nothing, never guess.
        vintage_file.unlink()
        check("unknown vintage reports nothing (never guesses)",
              app._rating_vintages([]), {})
        check("unknown vintage with unversioned rows still reports nothing",
              app._rating_vintages([{"name": "A", "srs": 16}]), {})

        # 6. A corrupt record must not raise — it degrades to unknown.
        vintage_file.write_text("{not json")
        check("corrupt record degrades to unknown", app._rating_vintages([]), {})

        # 7. Rows alone can supply the vintage when the file is absent.
        vintage_file.unlink()
        check("rows supply the vintage when the record is absent",
              app._rating_vintages([{"srs_source_year": 2025}]), {"srs": 2025})

        # 8. The shipped record must actually say SRS is a fallback season, because the
        #    whole point is that this fact stops being invisible.
        app.BASE_DIR = real_base
        real = json.loads((real_base / "data" / "rating_vintages.json").read_text())
        check("shipped record carries srs", real.get("srs"), 2025)
        app.RATING_SOURCE_YEARS["srs"] = 2025
        check("shipped record surfaces through the helper",
              app._rating_vintages([]).get("srs"), 2025)
    finally:
        app.BASE_DIR = real_base
        app.RATING_SOURCE_YEARS.clear()
        app.RATING_SOURCE_YEARS.update(saved_registry)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())