"""Live self-test for the D1 backfill write counter (CEO directive 2026-09-21).

Proves the counter of record is the D1 API RESPONSE, not a local guess:
 1. an upsert returns the D1-confirmed row count (response meta) and charges the
    daily write ledger exactly that many rows, and the rows are really in D1;
 2. a batch the API reports as 0 rows written raises d1_store.ConfirmedWriteError
    (no local fallback);
 3. a chunk that fetched source rows but confirmed 0 writes FAILS the run:
    rc=1, recorded under checkpoint["failed"], NOT marked done;
 4. a chunk whose source returned nothing is recorded as no_data and does not
    fail the run (and is still not marked done).

Run:  python tests/test_d1_counter.py
Requires the D1 token (read from Desktop/Cloudflare.txt) and live D1 access.
"""
import json
import os
import re
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["CF_D1_TOKEN"] = re.search(
    r"cfat_[A-Za-z0-9_\-]+",
    open(os.path.join(os.path.expanduser("~"), "Desktop", "Cloudflare.txt"),
         encoding="utf-8", errors="ignore").read(),
).group(0)
os.chdir(REPO)
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))
import d1_store           # noqa: E402
import backfill_d1 as bf  # noqa: E402

ok = []

# 1/2 ---------------------------------------------------------------- real write
before_led = d1_store.ledger_written()
before_n = d1_store.query("SELECT COUNT(*) AS n FROM teams")[0]["n"]
n = d1_store.upsert_teams([
    {"team_id": 9980001, "name": "ZZ Probe 1", "abbr": "ZP1", "conference": "T", "first_season": 2026},
    {"team_id": 9980002, "name": "ZZ Probe 2", "abbr": "ZP2", "conference": "T", "first_season": 2026},
])
meta = dict(d1_store.LAST_META)
after_led = d1_store.ledger_written()
rows = d1_store.query("SELECT COUNT(*) AS n FROM teams WHERE team_id >= 9980000")[0]["n"]
print(f"[1] upsert returned {n} (D1-confirmed) | upsert meta={meta} "
      f"| ledger delta={after_led - before_led} | rows visible in DB={rows}")
ok.append(("real write returns confirmed count", n == 2))
ok.append(("ledger charged exactly the confirmed rows", after_led - before_led == 2))
ok.append(("rows really are in D1", rows == 2))
d1_store.query("DELETE FROM teams WHERE team_id >= 9980000")
d1_store.query("DELETE FROM teams WHERE team_id >= 9990000")
left = d1_store.query("SELECT COUNT(*) AS n FROM teams")[0]["n"]
ok.append(("probe rows removed, table back to baseline", left == before_n))

# 3 ------------------------------------------------- zero-confirmed must raise
real_qf = d1_store.query_full
d1_store.query_full = lambda *a, **k: ([], {"rows_written": 0, "changes": 0})
try:
    d1_store.upsert_teams([{"team_id": 9970001, "name": "ZZ", "abbr": "ZZ",
                            "conference": "T", "first_season": 2026}])
    raised = False
except d1_store.ConfirmedWriteError:
    raised = True
d1_store.query_full = real_qf
print(f"[3] zero-confirmed batch raises ConfirmedWriteError: {raised}")
ok.append(("zero-confirmed batch raises", raised))
print(f"[3b] confirmed_writes({{}})={d1_store.confirmed_writes({})} (no local fallback)")
ok.append(("no local fallback", d1_store.confirmed_writes({}) == 0))

# 4 ------------------------------------------- run()-level: FAIL vs no_data
tmp = tempfile.mkdtemp(prefix="bfckpt_")
save_ckpt, save_lock, save_map = bf.CKPT, bf.LOCK, bf.load_name_map
bf.CKPT = os.path.join(tmp, "ck.json")
bf.LOCK = os.path.join(tmp, "lock")
bf.load_name_map = lambda s: None


def fake_fetch_none(season):
    return 0            # source had 0 rows (SRC_ROWS stays 0)


def fake_fetch_then_zero(season):
    bf.SRC_ROWS = 7     # the chunk DID fetch rows...
    return 0            # ...but confirmed no writes -> must FAIL


bf.JOBS = [("teams", fake_fetch_then_zero)]
rc = bf.run([2021])
ck = json.load(open(bf.CKPT, encoding="utf-8"))
print(f"[4] data-fetched-but-zero-confirmed -> rc={rc} done={ck['done']} failed={ck.get('failed')}")
ok.append(("zero-confirmed chunk FAILS the run", rc == 1))
ok.append(("zero-confirmed chunk NOT marked done", ck["done"] == []))
ok.append(("failure recorded in checkpoint", "2021:teams" in ck.get("failed", {})))

bf.JOBS = [("teams", fake_fetch_none)]
rc = bf.run([2021])
ck = json.load(open(bf.CKPT, encoding="utf-8"))
print(f"[5] empty-source chunk -> rc={rc} done={ck['done']} no_data={ck.get('no_data')}")
ok.append(("empty source does not fail the run", rc == 0))
ok.append(("empty source recorded as no_data", ck.get("no_data") == ["2021:teams"]))
ok.append(("empty source NOT marked done", ck["done"] == []))

bf.CKPT, bf.LOCK, bf.load_name_map = save_ckpt, save_lock, save_map

print("\n=== RESULT ===")
for name, passed in ok:
    print(f"  {'PASS' if passed else 'FAIL'}  {name}")
allpass = all(p for _, p in ok)
print("ALL PASS" if allpass else "*** SOME CHECKS FAILED ***")
sys.exit(0 if allpass else 1)