#!/usr/bin/env python3
"""selftest_served_snapshots.py — prove the served-state snapshot writer.

Why: QA could not establish the user-visible blast radius of the 2026-09-22
staleness incident because nothing retained what the site actually served. This
table is the fix, so the writer needs the same scrutiny as any other write path.

Uses a SYNTHETIC builder (not app.py) so the test is fast, deterministic, and does
not import the whole application or touch the network.

Asserts:
  1. table served_snapshots exists with the expected columns
  2. a forced snapshot writes and is readable with correct values
  3. change-driven: an unchanged payload writes NOTHING the second time
  4. a CHANGED payload writes again (the timeline actually grows)
  5. D1_WRITE_ENABLED gate still applies (off -> 0, nothing written)
  6. B4: an internal D1 failure never raises into the caller

Run:  python scripts/selftest_served_snapshots.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + ((" | " + str(detail)) if detail else ""))


def _fresh(env="1"):
    os.environ["D1_WRITE_ENABLED"] = env
    for m in ("d1_store", "d1_write_path"):
        sys.modules.pop(m, None)
    import d1_store
    import d1_write_path
    return d1_store, d1_write_path


EP = "/api/selftest-endpoint"
HASH_A = "a" * 32
HASH_B = "b" * 32


def _builder(h):
    return lambda: [{"endpoint": EP, "as_of": "2026-09-22T00:00:00Z",
                     "row_count": 25, "content_hash": h, "detail": "selftest"}]


def _rows_today(d1_store):
    return d1_store.query(
        "SELECT id, endpoint, as_of, row_count, content_hash, build_tag "
        "FROM served_snapshots WHERE endpoint = ? ORDER BY id", [EP])


def main():
    d1_store, dw = _fresh("1")

    # 1. schema
    cols = {r["name"] for r in d1_store.query("PRAGMA table_info(served_snapshots)")}
    want = {"id", "date", "ts_utc", "endpoint", "as_of", "row_count",
            "content_hash", "build_tag", "detail"}
    check("table served_snapshots exists with expected columns",
          want.issubset(cols), f"missing={sorted(want - cols)}" if cols else "no columns")

    # start from a clean slate for this endpoint
    d1_store.query(f"DELETE FROM served_snapshots WHERE endpoint = '{EP}'")

    # 2. forced write + readback
    n = dw.snapshot_served_state(_builder(HASH_A), force=True)
    rows = _rows_today(d1_store)
    check("forced snapshot writes > 0 rows", n > 0, f"returned {n}")
    if rows:
        r = rows[-1]
        check("row readable with correct endpoint/hash/count",
              r["endpoint"] == EP and r["content_hash"] == HASH_A and int(r["row_count"]) == 25,
              str(r))
        check("as_of is stored (the DATA timestamp, not request time)",
              r["as_of"] == "2026-09-22T00:00:00Z", repr(r["as_of"]))
    else:
        check("row readable with correct endpoint/hash/count", False, "no row")
        check("as_of is stored", False, "no row")

    # 3. unchanged -> no write
    before = len(_rows_today(d1_store))
    n2 = dw.snapshot_served_state(_builder(HASH_A))
    after = len(_rows_today(d1_store))
    check("change-driven: identical payload writes nothing",
          n2 == 0 and before == after, f"returned {n2} before={before} after={after}")

    # 4. changed -> writes again (the timeline grows)
    n3 = dw.snapshot_served_state(_builder(HASH_B))
    rows3 = _rows_today(d1_store)
    check("changed payload records a new state (timeline grows)",
          n3 > 0 and len(rows3) == before + 1 and rows3[-1]["content_hash"] == HASH_B,
          f"returned {n3} rows={len(rows3)}")

    # 5. gate OFF -> nothing written
    d1_store_g, dw_off = _fresh("0")
    before_off = len(_rows_today(d1_store_g))
    ret = dw_off.snapshot_served_state(_builder(HASH_A), force=True)
    after_off = len(_rows_today(d1_store_g))
    check("D1_WRITE_ENABLED=off writes nothing and returns 0",
          ret == 0 and before_off == after_off,
          f"ret={ret} before={before_off} after={after_off}")

    # 6. B4 — a failure deep in D1 must never reach the caller
    d1_store_b, dw_live = _fresh("1")
    orig = d1_store_b.append_served_snapshots

    def boom(*a, **k):
        raise RuntimeError("simulated D1 outage")

    dw_live.d1_store.append_served_snapshots = boom
    try:
        ret2 = dw_live.snapshot_served_state(_builder(HASH_B), force=True)
        check("B4: internal failure returns 0 and does not raise into the app", ret2 == 0,
              f"returned {ret2}")
    except Exception as e:
        check("B4: internal failure returns 0 and does not raise into the app", False,
              f"RAISED {type(e).__name__}: {e}")
    finally:
        d1_store_b.append_served_snapshots = orig

    # CLEAN UP — same reasoning as selftest_freshness_telemetry: these are real rows in
    # the prod served_snapshots timeline, and synthetic entries corrupt the very
    # blast-radius history the table exists to provide. The test tags its rows
    # detail='selftest'; delete exactly those.
    try:
        d1_store.query("DELETE FROM served_snapshots WHERE detail = 'selftest'")
        left = int(d1_store.query("SELECT COUNT(*) AS n FROM served_snapshots "
                                  "WHERE detail = 'selftest'")[0]["n"])
        print(f"  (cleaned synthetic snapshot rows: {left} left with detail='selftest')")
    except Exception as e:
        print(f"  (WARNING: could not clean synthetic rows: {e})")

    npass = sum(1 for _, ok, _ in results if ok)
    print(f"\n{npass}/{len(results)} PASS")
    return 0 if npass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())