"""Capture the D1 reads of the BOARD BUILD path (not just the serve path).

The serve path is measured by scripts/audit_d1_symmetry.py. This measures what a
board BUILD reads, which only runs when a board is stale — so a warm serve never
shows it. Writes are stubbed to no-ops so this can never touch production data.

Run:
    set -a; . ./.env; set +a
    export D1_WRITE_ENABLED=1
    python scripts/audit_d1_buildpath.py
"""
import os
import re
import sys
import json
import collections

os.environ.setdefault("REFRESH_INTERVAL_SECONDS", "0")
os.environ.setdefault("CFB_SKIP_BOOTWARM", "1")
os.environ.setdefault("D1_WRITE_ENABLED", "1")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import d1_store          # noqa: E402
import d1_write_path     # noqa: E402

READ_RE = re.compile(r'\b(?:FROM|JOIN)\s+["\'`]?(\w+)', re.I)
IGNORE = {"sqlite_master", "select", "where", "set", "values"}

captured: list[str] = []
_real_q, _real_qf = d1_store.query, d1_store.query_full


def _wrap(fn):
    def inner(sql, params=None, *a, **k):
        captured.append(sql)
        return fn(sql, params, *a, **k)
    return inner


d1_store.query = _wrap(_real_q)
d1_store.query_full = _wrap(_real_qf)

# hard no-op the board writers so a build cannot modify production
d1_write_path.store_slate = lambda *a, **k: 0
d1_store.save_slate = lambda *a, **k: 0
d1_write_path.snapshot_served_state = lambda *a, **k: 0
d1_store.append_served_snapshots = lambda *a, **k: 0

import app  # noqa: E402

out = {}
for label, fn in (("get_rankings(force=True)", lambda: app.get_rankings(force=True)),
                  ("load_schedule()", lambda: app.load_schedule()),
                  ("compute_win_totals(force=True)", lambda: app.compute_win_totals(force=True))):
    captured.clear()
    try:
        fn()
        status = "ok"
    except Exception as e:  # noqa: BLE001
        status = f"{type(e).__name__}: {e}"
    reads = collections.Counter()
    for sql in captured:
        for m in READ_RE.finditer(sql):
            t = m.group(1).lower()
            if t not in IGNORE:
                reads[t] += 1
    out[label] = {"status": status, "n_queries": len(captured),
                  "reads": dict(reads.most_common())}

print(json.dumps(out, indent=2))
