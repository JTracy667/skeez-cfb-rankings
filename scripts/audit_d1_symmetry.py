"""Audit D1 write/read symmetry on the LIVE serving path.

Empirical, not static: it wraps `d1_store.query` / `query_full` and records every
SQL statement the app actually issues while each public endpoint is served. So the
"read" column is evidence, not a guess from grep.

No CFBD calls (scheduler + bootwarm off), no writes.

Run:
    set -a; . ./.env; set +a
    export D1_WRITE_ENABLED=1
    python scripts/audit_d1_symmetry.py
"""
import os
import re
import sys
import json
import collections

os.environ.setdefault("REFRESH_INTERVAL_SECONDS", "0")   # no scheduler thread
os.environ.setdefault("CFB_SKIP_BOOTWARM", "1")          # no boot pull
os.environ.setdefault("D1_WRITE_ENABLED", "1")           # so D1 reads are enabled

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import d1_store  # noqa: E402

READ_RE = re.compile(r'\b(?:FROM|JOIN)\s+["\'`]?(\w+)', re.I)
WRITE_RE = re.compile(
    r'\b(INSERT\s+(?:OR\s+\w+\s+)?INTO|REPLACE\s+INTO|UPDATE|DELETE\s+FROM)\s+["\'`]?(\w+)', re.I)
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

import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app.app)

ENDPOINTS = ["/", "/analytics", "/schedule", "/win-totals",
             "/api/health", "/api/rankings", "/api/analytics", "/api/schedule"]

report = {}
for ep in ENDPOINTS:
    captured.clear()
    try:
        code = client.get(ep).status_code
    except Exception as e:  # noqa: BLE001
        code = f"ERR {type(e).__name__}: {e}"
    reads, writes = collections.Counter(), collections.Counter()
    for sql in captured:
        for m in WRITE_RE.finditer(sql):
            writes[m.group(2).lower()] += 1
        for m in READ_RE.finditer(sql):
            t = m.group(1).lower()
            if t not in IGNORE:
                reads[t] += 1
    report[ep] = {"status": code, "n_queries": len(captured),
                  "reads": dict(reads.most_common()), "writes": dict(writes.most_common())}

print(json.dumps(report, indent=2))
