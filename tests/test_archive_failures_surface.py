"""F5: a failed D1 archive must be VISIBLE, not swallowed.

Before this change `@_guard` caught every exception, printed it to a log the container
discards, and returned 0 -- so a dead archive was indistinguishable from a quiet one, and
the site could serve stale data with every probe still green.

These tests FORCE the failure rather than waiting for one, and they are hermetic: the D1
append is stubbed, so no production write happens.
"""


def test_guard_records_the_failure_in_process_and_in_d1(monkeypatch):
    import d1_write_path as w  # noqa: PLC0415

    recorded = []
    monkeypatch.setattr(w, "record_freshness_event", lambda **k: recorded.append(k) or 1)
    monkeypatch.setenv("D1_WRITE_ENABLED", "1")

    @w._guard
    def _boom(*a, **k):
        raise RuntimeError("simulated archive outage")

    before = w.archive_failure_state()["count"]
    assert _boom() == 0, "an archive failure must never affect the site"

    st = w.archive_failure_state()
    assert st["count"] == before + 1
    assert st["last_fn"] == "_boom"
    assert "simulated archive outage" in st["last_error"]
    assert recorded and recorded[-1]["event"] == "archive_failure"
    assert recorded[-1]["source"] == "_boom"


def test_recording_a_failure_cannot_itself_raise(monkeypatch):
    """The recorder runs INSIDE an exception handler. If it can raise, the failure of the
    recorder turns a swallowed write failure into a 500 on the serving path."""
    import d1_write_path as w  # noqa: PLC0415

    monkeypatch.setattr(w, "record_freshness_event",
                        lambda **k: (_ for _ in ()).throw(OSError("D1 unreachable")))
    monkeypatch.setenv("D1_WRITE_ENABLED", "1")

    @w._guard
    def _boom(*a, **k):
        raise ValueError("boom")

    assert _boom() == 0


def test_health_surfaces_the_archive_state():
    """A failure has to be reachable from the probe ops actually watch."""
    from fastapi.testclient import TestClient  # noqa: PLC0415

    import app as app_module  # noqa: PLC0415

    body = TestClient(app_module.app).get("/api/health").json()
    assert "archive" in body
    assert set(body["archive"]) >= {"count", "last_fn", "last_error", "last_ts"}


def test_health_archive_count_moves_when_a_write_fails(monkeypatch):
    """The end-to-end claim: force one archive failure and it becomes visible in
    /api/health -- the difference between a loud failure and the silent one we had."""
    from fastapi.testclient import TestClient  # noqa: PLC0415

    import app as app_module  # noqa: PLC0415
    import d1_write_path as w  # noqa: PLC0415

    monkeypatch.setattr(w, "record_freshness_event", lambda **k: 1)
    monkeypatch.setenv("D1_WRITE_ENABLED", "1")

    client = TestClient(app_module.app)
    before = client.get("/api/health").json()["archive"]["count"]

    @w._guard
    def _boom(*a, **k):
        raise RuntimeError("simulated outage")

    _boom()
    after = client.get("/api/health").json()["archive"]["count"]
    assert after == before + 1, "a failed archive did not show up in /api/health"