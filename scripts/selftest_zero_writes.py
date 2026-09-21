#!/usr/bin/env python3
"""selftest_zero_writes.py — proves the Phase-2 write-accounting contract.

Contract under test (CEO directive 2026-09-21):
  * a chunk whose source returned rows but whose D1 write CONFIRMED 0 rows must
    FAIL the chunk (recorded under `failed`, NOT marked `done`) and the run must
    exit non-zero;
  * a chunk whose source genuinely returned nothing is `no_data` (not a failure);
  * a chunk that confirmed writes IS marked done;
  * d1_store.confirmed_writes() trusts ONLY the D1 API response meta.

No network calls: the CFBD client and the D1 store are monkeypatched.

Usage:  python scripts/selftest_zero_writes.py
Exit 0 = all assertions passed.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))

import d1_store  # noqa: E402
import backfill_d1 as b  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}{(' — ' + detail) if detail else ''}")
    if not cond:
        FAILS.append(name)


def _fresh_paths():
    d = tempfile.mkdtemp(prefix="bf_selftest_")
    b.CKPT = os.path.join(d, "checkpoint.json")
    b.LOCK = os.path.join(d, "backfill.lock")
    if os.path.exists(b.LOCK):
        os.remove(b.LOCK)
    return d


def _run_teams(returned_rows, confirmed):
    """Run just the 2021:teams chunk with the source/D1 faked."""
    b.SEASONS = [2021]
    b.JOBS = [("teams", b.seed_teams)]
    b.cfbd_shared.cfbd_get = lambda path, **kw: list(returned_rows)
    b.d1_store.upsert_teams = lambda rows: (confirmed(rows) if callable(confirmed) else confirmed)
    return b.run([2021])


# ---------------------------------------------------------------- 1. confirmed_writes
check("confirmed_writes(None) == 0", d1_store.confirmed_writes(None) == 0)
check("confirmed_writes({}) == 0", d1_store.confirmed_writes({}) == 0)
check("confirmed_writes(meta.rows_written) == 7",
      d1_store.confirmed_writes({"rows_written": 7}) == 7,
      "the D1 API's own accounting wins")
check("confirmed_writes(meta.changes) == 5",
      d1_store.confirmed_writes({"changes": 5}) == 5)
check("confirmed_writes(no accounting key) == 0 — never a local guess",
      d1_store.confirmed_writes({"rows_read": 99}) == 0)

# ------------------------------------------- 2. source rows + 0 confirmed -> FAIL
_fresh_paths()
rc = _run_teams([{"id": 1, "school": "A"}, {"id": 2, "school": "B"}], 0)
ck = json.load(open(b.CKPT, encoding="utf-8"))
check("0-confirmed-writes chunk exits NON-ZERO", rc == 1, f"exit={rc}")
check("0-confirmed-writes chunk is NOT marked done",
      "2021:teams" not in ck.get("done", []), f"done={ck.get('done')}")
check("0-confirmed-writes chunk IS recorded under `failed`",
      "2021:teams" in ck.get("failed", {}),
      f"failed={ck.get('failed')}")

# ------------------------------------------------- 3. genuinely empty source -> no_data
_fresh_paths()
rc = _run_teams([], 0)
ck = json.load(open(b.CKPT, encoding="utf-8"))
check("empty source -> exit 0 (not a failure)", rc == 0, f"exit={rc}")
check("empty source -> recorded as no_data", "2021:teams" in ck.get("no_data", []))
check("empty source -> not in failed", "2021:teams" not in ck.get("failed", {}))

# ------------------------------------------------------------- 4. real writes -> done
_fresh_paths()
rc = _run_teams([{"id": 1, "school": "A"}, {"id": 2, "school": "B"}], lambda rows: len(rows))
ck = json.load(open(b.CKPT, encoding="utf-8"))
check("confirmed-write chunk exits 0", rc == 0, f"exit={rc}")
check("confirmed-write chunk marked done", "2021:teams" in ck.get("done", []))
check("checkpoint records the confirmed count",
      ck.get("chunks", {}).get("2021:teams", {}).get("confirmed") == 2,
      f"chunks={ck.get('chunks')}")
check("checkpoint rows_written reflects CONFIRMED writes",
      ck.get("rows_written") == 2, f"rows_written={ck.get('rows_written')}")

# ------------------------------------------- 5. exception in a chunk -> FAIL, abort
_fresh_paths()


def _boom(rows):
    raise RuntimeError("simulated D1 outage")


rc = _run_teams([{"id": 1, "school": "A"}], _boom)
ck = json.load(open(b.CKPT, encoding="utf-8"))
check("chunk exception exits non-zero", rc == 1, f"exit={rc}")
check("chunk exception is NOT marked done", "2021:teams" not in ck.get("done", []))
check("chunk exception recorded under failed", "2021:teams" in ck.get("failed", {}))

print()
if FAILS:
    print(f"SELFTEST FAILED ({len(FAILS)}): {FAILS}")
    raise SystemExit(1)
print("SELFTEST PASSED: zero-confirmed-write chunks fail and are never marked done.")
raise SystemExit(0)
