#!/usr/bin/env python3
"""selftest_freshness_state.py — durable last-pull state + the staleness guard.

Guards a REAL bug found 2026-09-22: the "last analytics pull" marker was a file that
(a) could not be durably updated (ephemeral container filesystem) and (b) was baked
into every image from the working tree, so the app reported a fixed ~41h-old value
and every fresh container believed it was due. That accidental eagerness was the only
thing masking a missed anchor.

Asserts:
  1. app_state round-trips (write -> read -> overwrite)
  2. get_last_pull_ts returns None when unset (not 0.0)
  3. D1 WINS over the local file  <-- the actual regression test
  4. guard is due when data is older than FRESHNESS_MAX_AGE_HOURS
  5. guard is NOT due when data is fresh
  6. guard is due when never pulled (-1.0 sentinel)
  7. FRESHNESS_GUARD=0 disables it (rollback path)
  8. B4: a D1 read failure during load never raises into the app

NOTE: this touches the REAL prod D1 app_state row, so it saves and restores the
original value in a finally block.

Run:  python scripts/selftest_freshness_state.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + ((" | " + str(detail)) if detail else ""))


def main():
    os.environ.setdefault("D1_WRITE_ENABLED", "1")
    os.environ["REFRESH_INTERVAL_SECONDS"] = "0"      # never start the daemon

    import d1_store
    import d1_write_path
    import app

    KEY = d1_write_path._LAST_PULL_KEY
    original = d1_store.get_app_state(KEY)

    try:
        # 1. round-trip
        d1_write_path.set_last_pull_ts(1_700_000_000.0)
        got = d1_store.get_app_state(KEY)
        check("app_state round-trips", got is not None and float(got) == 1_700_000_000.0,
              repr(got))
        d1_write_path.set_last_pull_ts(1_700_000_500.0)
        got2 = d1_store.get_app_state(KEY)
        check("app_state overwrites (upsert by key)",
              float(got2) == 1_700_000_500.0, repr(got2))

        # 2. unset -> None
        d1_store.query("DELETE FROM app_state WHERE key = ?", [KEY])
        check("get_last_pull_ts returns None when unset",
              d1_write_path.get_last_pull_ts() is None,
              repr(d1_write_path.get_last_pull_ts()))

        # 3. THE REGRESSION TEST: D1 must win over the file
        d1_ts = time.time() - 60                      # fresh: 1 minute ago
        file_ts = time.time() - (48 * 3600)           # stale: 48 hours ago
        d1_write_path.set_last_pull_ts(d1_ts)
        try:
            app.WEEKLY_ANALYTICS_FILE.write_text('{"ts": %r}' % file_ts)
            wrote_file = True
        except Exception as e:
            wrote_file = False
            print(f"  (could not write marker file: {e})")
        loaded = app._load_last_analytics_pull()
        check("D1 WINS over the stale local file (the actual bug)",
              abs(loaded - d1_ts) < 5,
              f"loaded={loaded} d1={d1_ts} file={file_ts} wrote_file={wrote_file}")
        age = app._analytics_age_hours()
        check("age is computed from the durable value, not the file",
              age >= 0 and age < 1.0, f"age_hours={age:.3f}")

        # 4/5. guard thresholds
        app.FRESHNESS_GUARD_ON = True
        app.FRESHNESS_MAX_AGE_HOURS = 12.0
        check("guard NOT due when data is fresh", app.freshness_guard_due() is False,
              f"age={app._analytics_age_hours():.2f}h")
        d1_write_path.set_last_pull_ts(time.time() - (13 * 3600))
        check("guard IS due when older than max age", app.freshness_guard_due() is True,
              f"age={app._analytics_age_hours():.2f}h limit=12h")

        # 6. never pulled
        d1_store.query("DELETE FROM app_state WHERE key = ?", [KEY])
        try:
            app.WEEKLY_ANALYTICS_FILE.unlink()
        except Exception:
            pass
        check("guard IS due when never pulled (-1.0 sentinel)",
              app._analytics_age_hours() == -1.0 and app.freshness_guard_due() is True,
              f"age={app._analytics_age_hours()}")

        # 7. rollback switch
        app.FRESHNESS_GUARD_ON = False
        check("FRESHNESS_GUARD=0 disables the guard (rollback path)",
              app.freshness_guard_due() is False, "")
        app.FRESHNESS_GUARD_ON = True

        # 8. B4 — read failure must not raise
        orig = d1_write_path.d1_store.get_app_state
        d1_write_path.d1_store.get_app_state = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("simulated D1 read outage"))
        try:
            app._load_last_analytics_pull()
            check("B4: D1 read failure falls back without raising", True, "")
        except Exception as e:
            check("B4: D1 read failure falls back without raising", False,
                  f"RAISED {type(e).__name__}: {e}")
        finally:
            d1_write_path.d1_store.get_app_state = orig

    finally:
        # restore real prod state
        if original is None:
            d1_store.query("DELETE FROM app_state WHERE key = ?", [KEY])
        else:
            d1_store.set_app_state(KEY, original)
        print(f"\n  (restored app_state[{KEY}] = {original!r})")

    npass = sum(1 for _, ok, _ in results if ok)
    print(f"\n{npass}/{len(results)} PASS")
    return 0 if npass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())