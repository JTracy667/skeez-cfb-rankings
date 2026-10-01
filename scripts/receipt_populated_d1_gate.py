"""Produce an independently READABLE receipt for the populated-D1 gate and the write ledger.

QA's position: my populated-database pass and the unchanged ledger were CTO-reported, not things
they could read back. They asked for a safe, independently readable receipt -- or for those items
to be carried explicitly as unverified in the release decision.

What this produces (written to docs/evidence/):
  * the exact commands and environment (database id, token FINGERPRINT -- never the token),
  * the raw pytest output of the served-week gate against the populated database,
  * the raw rows of the query the gate asserts on, so the DATA is visible and not just a verdict,
  * the ledger value and its sha256 BEFORE and AFTER, so a reader can confirm the counter did not
    move, and can hash the file themselves,
  * an explicit statement of what remains unverifiable without a D1 read credential.

Read-only by construction: SELECT statements only, through the repo's own query path. The conftest
guard refuses production writes during the test run, and D1_LEDGER_DISABLED keeps the ledger
untouched by test traffic.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time

REPO = r"C:\Users\jtracy\dev\cfb-power-rankings"
LEDGER = os.path.join(os.environ["LOCALAPPDATA"], "hermes", "d1_write_ledger.json")
OUT_DIR = os.path.join(REPO, "docs", "evidence")
PROD_D1 = "c3ec3149-cc85-483b-b727-5a18e3d5a1b9"
SEASON = 2026


def ledger_state() -> dict:
    raw = open(LEDGER, "rb").read()
    try:
        val = json.loads(raw).get("rows_written")
    except Exception:
        val = None
    return {"path": LEDGER, "rows_written": val, "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw), "mtime_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                          time.gmtime(os.path.getmtime(LEDGER)))}


def run(cmd: list[str], env: dict) -> dict:
    p = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, env=env, timeout=600)
    return {"cmd": " ".join(cmd), "rc": p.returncode,
            "stdout": p.stdout.strip(), "stderr": p.stderr.strip()[-2000:]}


def main() -> int:
    token = os.environ.get("CF_D1_TOKEN") or ""
    fp = hashlib.sha256(token.encode()).hexdigest()[:12] if token else "(none)"
    env = dict(os.environ)
    env["CF_D1_DB_ID"] = PROD_D1

    before = ledger_state()

    gate = run([sys.executable, "-m", "pytest", "-q", "-p", "no:randomly",
                "tests/test_served_equals_d1.py::test_d1_results_keep_up_with_the_served_week",
                "--tb=short"], env)

    # The rows the gate asserts on, read directly, so a reader sees the DATA not just the verdict.
    q = ("SELECT MAX(week) AS newest_scored_week, COUNT(*) AS scored_games "
         "FROM games WHERE season=%d AND home_score IS NOT NULL" % SEASON)
    probe = run([sys.executable, "-c",
                 "import os,sys,json;"
                 "sys.path.insert(0, r'%s');"
                 "import d1_store;"
                 "print(json.dumps(d1_store.query(%r)))" % (REPO, q)], env)

    after = ledger_state()

    receipt = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "branch": subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=REPO,
                                 capture_output=True, text=True).stdout.strip(),
        "commit": subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO,
                                 capture_output=True, text=True).stdout.strip(),
        "database_id": PROD_D1,
        "database_label": "production (cfb-history)",
        "token_fingerprint_sha256_12": fp,
        "access": "READ-ONLY: SELECT statements only; conftest refuses production writes in tests",
        "gate": gate,
        "gate_query": q,
        "gate_query_result": probe,
        "ledger_before": before,
        "ledger_after": after,
        "ledger_moved": before["rows_written"] != after["rows_written"],
        "unverifiable_without_a_credential": [
            "That the gate ran against THIS database (a reader without a D1 credential cannot "
            "re-execute it; this file is the raw output of the run, not a reproducible recipe).",
            "That the ledger value reflects D1 row counts -- it is a local counter; a reader can "
            "read and hash the file, but not independently recompute the number.",
        ],
    }

    os.makedirs(OUT_DIR, exist_ok=True)
    json_path = os.path.join(OUT_DIR, "2026-10-01-populated-d1-gate.json")
    md_path = os.path.join(OUT_DIR, "2026-10-01-populated-d1-gate.md")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(receipt, f, indent=2)

    md = f"""# Populated-D1 gate + write-ledger receipt (CTO-reported, independently readable)

Generated {receipt['generated_utc']} on branch `{receipt['branch']}` at `{receipt['commit'][:12]}`.

**What this is.** QA asked for a safe, independently readable receipt for two things that were
previously only CTO-reported: the served-week gate passing against the populated database, and the
D1 write ledger not moving. This file is the raw output of both, with hashes a reader can confirm.

**What it is not.** It is not a substitute for running the gate yourself. Without a D1 read
credential the run cannot be re-executed, and the ledger is a local counter -- readable and
hashable, but not independently recomputable. If the release decision needs these independently
verified rather than read back, QA needs a READ-ONLY D1 credential (or Jeff's call to carry them as
unverified).

## Environment

| | |
|---|---|
| database | `{receipt['database_id']}` (production `cfb-history`) |
| access | {receipt['access']} |
| token fingerprint | sha256[:12] = `{fp}` (value never recorded) |

## Gate

```
$ {gate['cmd']}
(rc={gate['rc']})
{gate['stdout']}
{gate['stderr']}
```

## The rows the gate asserts on (read directly, so the data is visible too)

```
$ {probe['cmd']}
(rc={probe['rc']})
{probe['stdout']}
{probe['stderr']}
```

## Write ledger

| | before | after |
|---|---|---|
| rows_written | {before['rows_written']} | {after['rows_written']} |
| sha256 | `{before['sha256']}` | `{after['sha256']}` |
| mtime (UTC) | {before['mtime_utc']} | {after['mtime_utc']} |

Ledger moved: **{receipt['ledger_moved']}**. Path (readable directly on this machine):
`{LEDGER}` -- a reader can `sha256sum` it and compare.

## Carried as unverified

""" + "\n".join(f"- {u}" for u in receipt["unverifiable_without_a_credential"]) + "\n"

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)

    print(f"gate rc={gate['rc']} | ledger {before['rows_written']} -> {after['rows_written']} "
          f"(moved={receipt['ledger_moved']})")
    print(f"wrote {md_path}")
    print(f"wrote {json_path}")
    print(f"ledger sha256 after: {after['sha256']}")
    return 0 if gate["rc"] == 0 and not receipt["ledger_moved"] else 1


if __name__ == "__main__":
    raise SystemExit(main())