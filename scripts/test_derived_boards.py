"""Equivalence test for the derived boards (rankings + win totals).

The whole point of the change is that the stored board must be INDISTINGUISHABLE from a
fresh compute — if it isn't, serving it is a silent data regression. So: compute each
board once, read it back from D1, and diff every field.

Deliberately ONE pass. Local runs spend the same metered CFBD quota the site uses, and
the earlier quota leak happened precisely because test boots re-bought shared data.

Rows written here are tagged for cleanup (scripts/reset_derived_boards.py) because D1 is
production: this box's live-week injuries/weather must not be what prod serves.

Usage:  python scripts/test_derived_boards.py
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")

# D1 credentials live in .env and are read from the ENV at call time, so a bare script
# silently runs with the durable store disabled and every read returns None. (That is
# exactly how this test first "failed": it looked like the store was broken, but D1 was
# simply switched off in-process.)
_ENV = Path(__file__).resolve().parent.parent / ".env"
if _ENV.exists():
    for _line in _ENV.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

import app  # noqa: E402

YEAR = app.CFBD_YEAR
report = {}


def _diff(a, b, path="") -> list[str]:
    """Field-level differences between two JSON structures (shallow for lists)."""
    out = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append(f"{path}.{k}: missing on fresh")
            elif k not in b:
                out.append(f"{path}.{k}: missing on served")
            elif a[k] != b[k]:
                out.append(f"{path}.{k}: {a[k]!r} != {b[k]!r}")
    elif a != b:
        out.append(f"{path}: {a!r} != {b!r}")
    return out


# ---- rankings -------------------------------------------------------------
t0 = time.time()
fresh_rank = app.get_rankings().model_dump()      # computes AND stores (see get_rankings)
report["rankings_compute_s"] = round(time.time() - t0, 2)

t0 = time.time()
row = app._board_from_store(app.BOARD_RANKINGS, YEAR)
report["rankings_store_read_s"] = round(time.time() - t0, 2)
served_rank = app._public_board(row) if row else {}
rd = _diff(fresh_rank, served_rank)
report["rankings"] = {
    "teams_fresh": len(fresh_rank.get("teams") or []),
    "teams_served": len(served_rank.get("teams") or []),
    "identical": not rd,
    "diffs": rd[:6],
    "fingerprint": (row or {}).get("fingerprint"),
}

# ---- win totals -----------------------------------------------------------
t0 = time.time()
fresh_wt = app.compute_win_totals(YEAR, force=True)
report["win_totals_compute_s"] = round(time.time() - t0, 2)
app._store_board(app.BOARD_WIN_TOTALS, fresh_wt, YEAR)

t0 = time.time()
row = app._board_from_store(app.BOARD_WIN_TOTALS, YEAR)
report["win_totals_store_read_s"] = round(time.time() - t0, 2)
served_wt = app._public_board(row) if row else {}
wd = _diff(fresh_wt, served_wt)
report["win_totals"] = {
    "teams_fresh": len(fresh_wt.get("teams") or []),
    "teams_served": len(served_wt.get("teams") or []),
    "identical": not wd,
    "diffs": wd[:6],
    "fingerprint": (row or {}).get("fingerprint"),
}

# ---- endpoint path (must hit the store, same fingerprint -> no recompute) --
t0 = time.time()
ep = app.api_win_totals()
report["endpoint_win_totals_s"] = round(time.time() - t0, 2)
report["endpoint_matches_fresh"] = (ep == fresh_wt)

# ---- the change detector must be STABLE when nothing moved ---------------
fp1 = app._board_fingerprint(app.BOARD_WIN_TOTALS, YEAR)
fp2 = app._board_fingerprint(app.BOARD_WIN_TOTALS, YEAR)
report["fingerprint_stable"] = (fp1 == fp2 and bool(fp1))
report["results_signature_present"] = bool(app._results_signature(YEAR))

print(json.dumps(report, indent=2, default=str))