"""Delete the derived-board rows a LOCAL test run wrote, so prod builds its own.

D1 is production. A board built on this box carries this box's live-week injuries and
weather, which is not what prod should serve — the same reason the slate row had to be
removed after the v41 test. Scoped by an explicit kind list (never by a numeric range:
CFBD ids are ~40xxxxxxx, and a low-threshold range delete once wiped a table).

Usage:  D1_WRITE_ENABLED=1 python scripts/reset_derived_boards.py
"""
import os
import sys
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
import d1_store  # noqa: E402

KINDS = ["rankings", "win_totals"]
season = app.CFBD_YEAR

before = d1_store.query(
    "SELECT kind, season, week, built_at, fingerprint FROM slate_cache ORDER BY kind")
print("rows before:")
for r in before:
    print("  ", r)

n = 0
for kind in KINDS:
    # query_full is the D1 HTTP path that carries writes (query() is read-only shape).
    _rows, _meta = d1_store.query_full(
        "DELETE FROM slate_cache WHERE kind = ? AND season = ?", [kind, season])
    n += int((_meta or {}).get("changes") or 0)
print(f"deleted {n} local test row(s) for {KINDS} season {season}")

after = d1_store.query(
    "SELECT kind, season, week, built_at FROM slate_cache ORDER BY kind")
print("rows after:")
for r in after:
    print("  ", r)