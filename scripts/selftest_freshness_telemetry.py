#!/usr/bin/env python3
"""selftest_freshness_telemetry.py — prove the new freshness telemetry works.

Why this exists: the 2026-09-22 staleness incident could not be diagnosed after
the fact, because the only evidence was a console.log Cloudflare does not retain.
QA returned INCONCLUSIVE for both the freshness SLA and the cold-start self-heal
for exactly that reason. This table is the fix, so it needs the same scrutiny as
any other write path.

Asserts:
  1. the table exists with the expected columns
  2. a write confirms > 0 rows and is readable with correct field values
  3. age_hours tolerates the "never pulled" sentinel (-1.0)
  4. detail is truncated to <= 500 chars
  5. the D1_WRITE_ENABLED gate still applies (off -> 0 rows, nothing written)
  6. B4: an internal D1 failure NEVER raises into the caller and never breaks the site

Run:  python scripts/selftest_freshness_telemetry.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + ((" | " + str(detail)) if detail else ""))


def _fresh(env="1"):
    """Reload modules so env changes take effect."""
    os.environ["D1_WRITE_ENABLED"] = env
    for m in ("d1_store", "d1_write_path"):
        sys.modules.pop(m, None)
    import d1_store
    import d1_write_path
    return d1_store, d1_write_path


def main():
    d1_store, d1_write_path = _fresh("1")

    # 1. schema
    try:
        cols = {r["name"] for r in d1_store.query("PRAGMA table_info(freshness_events)")}
    except Exception as e:
        cols = set()
        print(f"  (PRAGMA failed: {e})")
    want = {"id", "ts_utc", "event", "source", "age_hours", "build_tag", "detail"}
    check("table freshness_events exists with expected columns",
          want.issubset(cols), f"missing={sorted(want - cols)}" if cols else "no columns")

    # 2. write + read back
    marker = "selftest-telemetry"
    n = d1_write_path.record_freshness_event(
        "container_start", "selftest", age_hours=-1.0, detail=marker)
    check("write confirms > 0 rows", n > 0, f"returned {n}")
    rows = d1_store.query(
        "SELECT ts_utc, event, source, age_hours, build_tag, detail FROM freshness_events "
        "WHERE source = 'selftest' ORDER BY id DESC LIMIT 1")
    if rows:
        r = rows[0]
        check("row readable with correct event/source",
              r["event"] == "container_start" and r["source"] == "selftest", str(r))
        check("ts_utc is populated", bool(r["ts_utc"]), repr(r["ts_utc"]))
        # 3. sentinel
        check("age_hours stores the -1.0 'never pulled' sentinel",
              r["age_hours"] is not None and float(r["age_hours"]) == -1.0,
              repr(r["age_hours"]))
    else:
        check("row readable with correct event/source", False, "no row found")
        check("ts_utc is populated", False, "no row found")
        check("age_hours stores the -1.0 sentinel", False, "no row found")

    # 4. truncation
    d1_write_path.record_freshness_event("pull_failed", "selftest", detail="x" * 900)
    r2 = d1_store.query(
        "SELECT LENGTH(detail) AS n FROM freshness_events WHERE source = 'selftest' "
        "ORDER BY id DESC LIMIT 1")
    check("detail truncated to <= 500 chars",
          bool(r2) and int(r2[0]["n"]) <= 500, f"len={r2[0]['n'] if r2 else '?'}")

    # 5. gate OFF -> nothing written
    d1_store, d1_write_path_gated = _fresh("0")
    before = int(d1_store.query(
        "SELECT COUNT(*) AS n FROM freshness_events")[0]["n"])
    ret = d1_write_path_gated.record_freshness_event("container_start", "selftest-off")
    after = int(d1_store.query(
        "SELECT COUNT(*) AS n FROM freshness_events")[0]["n"])
    check("D1_WRITE_ENABLED=off writes nothing and returns 0",
          ret == 0 and before == after, f"ret={ret} before={before} after={after}")

    # 6. B4 — a failure deep in D1 must never reach the caller
    d1_store2, d1_write_path_live = _fresh("1")
    orig = d1_store2.append_freshness_events

    def boom(*a, **k):
        raise RuntimeError("simulated D1 outage")

    d1_store2.append_freshness_events = boom
    d1_write_path_live.d1_store.append_freshness_events = boom
    try:
        ret2 = d1_write_path_live.record_freshness_event("container_start", "selftest-boom")
        check("B4: internal failure returns 0 and does not raise into the app", ret2 == 0,
              f"returned {ret2}")
    except Exception as e:
        check("B4: internal failure returns 0 and does not raise into the app", False,
              f"RAISED {type(e).__name__}: {e}")
    finally:
        d1_store2.append_freshness_events = orig

    # CLEAN UP — this test writes REAL rows into the prod freshness table, and leaving
    # them behind is not cosmetic: they are indistinguishable from genuine container
    # starts to anything reading the table. A watcher of mine was fooled by exactly
    # these rows on 2026-09-22 and reported a false regression verdict off them.
    try:
        d1_store.query("DELETE FROM freshness_events WHERE source LIKE 'selftest%'")
        left = int(d1_store.query("SELECT COUNT(*) AS n FROM freshness_events "
                                  "WHERE source LIKE 'selftest%'")[0]["n"])
        print(f"  (cleaned synthetic freshness rows: {left} left with source LIKE 'selftest%')")
    except Exception as e:
        print(f"  (WARNING: could not clean synthetic rows: {e})")

    npass = sum(1 for _, ok, _ in results if ok)
    print(f"\n{npass}/{len(results)} PASS")
    return 0 if npass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())