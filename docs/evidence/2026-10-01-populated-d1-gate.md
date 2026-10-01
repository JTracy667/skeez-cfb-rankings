# Populated-D1 gate + write-ledger receipt (CTO-reported, independently readable)

Generated 2026-10-01T15:11:32Z on branch `perf/d1-snapshot-safety` at `2af4defa6d99`.

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
| database | `c3ec3149-cc85-483b-b727-5a18e3d5a1b9` (production `cfb-history`) |
| access | READ-ONLY: SELECT statements only; conftest refuses production writes in tests |
| token fingerprint | sha256[:12] = `fe44a850fceb` (value never recorded) |

## Gate

```
$ C:\Users\jtracy\AppData\Local\hermes\tools\python-3.14.7+20260901-win32-x64\python.exe -m pytest -q -p no:randomly tests/test_served_equals_d1.py::test_d1_results_keep_up_with_the_served_week --tb=short
(rc=0)
.                                                                        [100%]
1 passed in 6.76s

```

## The rows the gate asserts on (read directly, so the data is visible too)

```
$ C:\Users\jtracy\AppData\Local\hermes\tools\python-3.14.7+20260901-win32-x64\python.exe -c import os,sys,json;sys.path.insert(0, r'C:\Users\jtracy\dev\cfb-power-rankings');import d1_store;print(json.dumps(d1_store.query('SELECT MAX(week) AS newest_scored_week, COUNT(*) AS scored_games FROM games WHERE season=2026 AND home_score IS NOT NULL')))
(rc=0)
[{"newest_scored_week": 4, "scored_games": 1348}]

```

## Write ledger

| | before | after |
|---|---|---|
| rows_written | 48173 | 48173 |
| sha256 | `479f709722fe7974fa185e18ceb21fe9d32bf6d95f096c087dc079a50600d350` | `479f709722fe7974fa185e18ceb21fe9d32bf6d95f096c087dc079a50600d350` |
| mtime (UTC) | 2026-10-01T03:25:50Z | 2026-10-01T03:25:50Z |

Ledger moved: **False**. Path (readable directly on this machine):
`C:\Users\jtracy\AppData\Local\hermes\d1_write_ledger.json` -- a reader can `sha256sum` it and compare.

## Carried as unverified

- That the gate ran against THIS database (a reader without a D1 credential cannot re-execute it; this file is the raw output of the run, not a reproducible recipe).
- That the ledger value reflects D1 row counts -- it is a local counter; a reader can read and hash the file, but not independently recompute the number.
