"""The archive-failure recorder must report ONE failure: no recursion, no inflated count, and
the ORIGINAL error must survive.

Production 2026-10-01: the D1 write path went down, and the recorder that exists to make that
visible instead recursed (guard -> recorder -> guarded recorder -> ...) until RecursionError.
`/api/health` reported archive.count 3450+ with last_fn `record_freshness_event`, D1 had zero
`archive_failure` rows, and the real cause was never visible. The busy loop also kept the
container from idling, so it never recycled.

Run: python -m pytest tests/test_archive_failure_recorder.py -q
"""
import d1_write_path as w


def test_recorder_counts_one_failure_and_does_not_recurse(monkeypatch):
    """The write path is down: recording a freshness event fails, which used to call the
    recorder again, which recorded again... Assert it stops, and stops at ONE."""
    def boom(*a, **k):
        raise RuntimeError("D1 unreachable after retries")

    monkeypatch.setattr(w.d1_store, "append_freshness_events", boom)
    monkeypatch.setattr(w, "enabled", lambda: True)

    before = w.archive_failure_state()["count"]
    w._record_archive_failure("snapshot_team_analytics", RuntimeError("the real cause"))
    after = w.archive_failure_state()["count"]

    assert after - before == 1, (
        f"one failure must count exactly once, got {after - before} "
        "(the recorder recursed or the count was inflated)")
    state = w.archive_failure_state()
    assert state["last_fn"] == "snapshot_team_analytics", (
        "the recorder's own failure overwrote the original function name")
    assert "the real cause" in (state["last_error"] or ""), (
        "the recorder's own error overwrote the ORIGINAL error, which is the only clue to the cause")


def test_recorder_never_raises(monkeypatch):
    """It runs inside an exception handler: telemetry must never be able to affect the site."""
    def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(w.d1_store, "append_freshness_events", boom)
    monkeypatch.setattr(w, "enabled", lambda: True)
    w._record_archive_failure("some_fn", ValueError("x"))   # must not raise


def test_a_guarded_write_failure_records_exactly_once(monkeypatch):
    """End to end through the decorator: a guarded function failing while the store is down
    must produce one recorded failure, not a recursion."""
    def boom(*a, **k):
        raise RuntimeError("D1 unreachable after retries")

    monkeypatch.setattr(w.d1_store, "append_freshness_events", boom)
    monkeypatch.setattr(w, "enabled", lambda: True)

    @w._guard
    def _archive_something():
        raise RuntimeError("the real cause")

    before = w.archive_failure_state()["count"]
    assert _archive_something() == 0          # the guard still returns 0, site unaffected
    after = w.archive_failure_state()["count"]

    assert after - before == 1, f"expected one recorded failure, got {after - before}"
    assert "the real cause" in (w.archive_failure_state()["last_error"] or "")