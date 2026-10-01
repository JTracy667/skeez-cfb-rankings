"""QA remediation §3/§4 — the serving fallback chain and its reported state.

§3: a missing/invalid publication marker must NOT suppress the documented disk fallback.
    (Before this, the disk file was loaded only when the identity map was empty, so a marker
    that was absent or malformed turned a valid fallback into an empty payload.)
§4: the numeric universe is the PUBLISHED snapshot, identity is joined by team name, and a
    response that mixes snapshots is reported as degraded instead of rendered silently.

No network, no D1: the two D1 entry points are monkeypatched, which is what makes these
tests hermetic. The disk file is stubbed too, so the repository's data/ is never touched.
"""
import app


def _stub_d1(monkeypatch, *, pub=None, rows=None, raises=None, identity_rows=()):
    monkeypatch.setattr(app.d1_write_path, "analytics_publication", lambda season: pub)
    monkeypatch.setattr(app.d1_write_path, "team_identity_rows", lambda: list(identity_rows))

    def _load(season, pub=None):
        if raises is not None:
            raise raises
        return list(rows or [])

    monkeypatch.setattr(app.d1_write_path, "load_team_analytics", _load)


GEORGIA = {"name": "Georgia", "sp_plus": 30.2, "mascot": "Bulldogs", "conf": "SEC"}


def test_missing_marker_with_identity_present_still_serves_disk_numerics(monkeypatch):
    """The exact §3 defect: identity non-empty + no marker used to return numerics-free rows."""
    _stub_d1(monkeypatch, pub=None, rows=[])
    monkeypatch.setattr(app, "_load_cfbd_analytics_file", lambda: [dict(GEORGIA)])
    monkeypatch.setattr(app, "_analytics_identity_map",
                        lambda pub=None: {"Georgia": {"mascot": "Bulldogs", "conf": "SEC"}})

    out = app._served_analytics()

    assert [t["name"] for t in out] == ["Georgia"]
    assert out[0]["sp_plus"] == 30.2, "the fallback's numerics must survive a missing marker"
    assert app._ANALYTICS_SERVE["degraded"] is True, "a fallback serve must say it is degraded"
    assert "no verified publication" in app._ANALYTICS_SERVE["source"]


def test_no_marker_and_no_identity_still_serves_disk_numerics(monkeypatch):
    _stub_d1(monkeypatch, pub=None, rows=[])
    monkeypatch.setattr(app, "_load_cfbd_analytics_file", lambda: [dict(GEORGIA)])
    monkeypatch.setattr(app, "_analytics_identity_map", lambda pub=None: {})

    out = app._served_analytics()
    assert out[0]["sp_plus"] == 30.2
    assert app._ANALYTICS_SERVE["degraded"] is True


def test_nothing_anywhere_reports_explicitly_empty_not_fresh(monkeypatch):
    _stub_d1(monkeypatch, pub=None, rows=[])
    monkeypatch.setattr(app, "_load_cfbd_analytics_file", lambda: [])
    monkeypatch.setattr(app, "_analytics_identity_map", lambda pub=None: {})

    assert app._served_analytics() == []
    assert app._ANALYTICS_SERVE["source"] == "empty"
    assert app._ANALYTICS_SERVE["degraded"] is True


def test_d1_read_failure_falls_back_to_disk_and_reports_it(monkeypatch):
    _stub_d1(monkeypatch, raises=RuntimeError("d1 down"))
    monkeypatch.setattr(app, "_load_cfbd_analytics_file", lambda: [dict(GEORGIA)])
    monkeypatch.setattr(app, "_analytics_identity_map", lambda pub=None: {})

    out = app._served_analytics()
    assert out[0]["sp_plus"] == 30.2
    assert app._ANALYTICS_SERVE["degraded"] is True
    assert "d1 read failed" in app._ANALYTICS_SERVE["source"]


def test_building_a_board_refuses_the_disk_fallback(monkeypatch):
    """A board is written into D1: it must come from a verified publication or not at all."""
    _stub_d1(monkeypatch, pub=None, rows=[])
    monkeypatch.setattr(app, "_load_cfbd_analytics_file", lambda: [dict(GEORGIA)])
    monkeypatch.setattr(app, "_analytics_identity_map", lambda pub=None: {})

    app._VERIFIED_ONLY = True
    try:
        assert app._served_analytics() == [], "the fallback must not feed a board"
    finally:
        app._VERIFIED_ONLY = False
    assert app._ANALYTICS_SERVE["degraded"] is True


def test_verified_publication_reports_d1_and_not_degraded(monkeypatch):
    _stub_d1(monkeypatch, pub={"stamp": "2026-09-30T16:18:36Z", "week": 5,
                               "identity": {"Georgia": {"mascot": "Bulldogs", "conf": "SEC"}}, "digest": "dd"},
             rows=[{"name": "Georgia", "sp_plus": 30.2}])
    monkeypatch.setattr(app, "_analytics_identity_map",
                        lambda pub=None: {"Georgia": {"mascot": "Bulldogs", "conf": "SEC"}})
    monkeypatch.setattr(app, "_load_cfbd_analytics_file",
                        lambda: (_ for _ in ()).throw(AssertionError("must not read disk")))

    out = app._served_analytics()
    assert out[0]["sp_plus"] == 30.2
    assert app._ANALYTICS_SERVE == {"source": "d1", "degraded": False, "reason": ""}


def test_rows_outside_the_published_universe_are_reported_not_hidden(monkeypatch):
    """§4: two snapshots must never be mixed into one response without saying so."""
    _stub_d1(monkeypatch, pub={"stamp": "s", "week": 5, "identity": {"Georgia": {}}},
             rows=[{"name": "Georgia", "sp_plus": 30.2}, {"name": "Alabama", "sp_plus": 25.0}])
    monkeypatch.setattr(app, "_analytics_identity_map", lambda pub=None: {"Georgia": {}})

    app._served_analytics()
    assert app._ANALYTICS_SERVE["degraded"] is True
    assert "mixed universe" in app._ANALYTICS_SERVE["source"]


def test_opt_out_does_zero_d1_reads_and_is_reported_degraded(monkeypatch):
    calls = []
    monkeypatch.setattr(app.d1_write_path, "analytics_publication",
                        lambda season: calls.append("marker"))
    monkeypatch.setattr(app, "_load_cfbd_analytics_file", lambda: [dict(GEORGIA)])
    monkeypatch.setenv("ANALYTICS_FROM_D1", "0")

    out = app._served_analytics()
    assert calls == [], "the opt-out must short-circuit before any D1 read"
    assert out[0]["sp_plus"] == 30.2
    assert "ANALYTICS_FROM_D1=0" in app._ANALYTICS_SERVE["source"]
    assert app._ANALYTICS_SERVE["degraded"] is True

def test_publication_with_rows_but_no_live_identity_serves_without_crashing(monkeypatch):
    """QA counterexample 1: publication + numeric rows + NO live identity map.

    This is the path that exited 1 with `UnboundLocalError: ... 'disk'` at app.py:4588 --
    the disk fallback was refactored into a lazy helper, but the success path still read the
    old `disk` local. Serving must never raise.
    """
    _stub_d1(monkeypatch, pub={"stamp": "2026-09-30T16:18:36Z", "week": 5, "digest": "dd",
                 "identity": {}},
             rows=[{"name": "Georgia", "sp_plus": 30.2}])
    monkeypatch.setattr(app, "_analytics_identity_map", lambda pub=None: {})
    monkeypatch.setattr(app, "_load_cfbd_analytics_file",
                        lambda: [{"name": "Georgia", "mascot": "Bulldogs", "conf": "SEC",
                                  "sp_plus": 30.2}])

    out = app._served_analytics()

    assert out[0]["name"] == "Georgia"
    assert out[0]["sp_plus"] == 30.2, "the published numeric row must be served"
    assert out[0]["mascot"] == "Bulldogs", "string fields come from the disk fallback"
    assert app._ANALYTICS_SERVE["degraded"] is False, "the numerics were verified"
