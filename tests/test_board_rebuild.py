"""The stored-board rebuild path: a board must not read the store it is replacing.

WHY THIS EXISTS: `_refresh_boards_if_stale` used to clear only the in-memory rankings
cache and then call `get_rankings()`, which serves the DURABLE board first. So the
"rebuild" read the very payload it was meant to replace and re-stored it under the new
fingerprint — after which every later check reported "unchanged" and the board was frozen
on Wednesday's numbers while producing a healthy-looking refresh log. The live symptom was
Georgia showing AP 2 / Coaches 2 / 3-0 with a board `updated` of 2026-09-24 while the polls
were out and Saturday's games were final.

Hermetic: no network, no D1. Every collaborator get_rankings() touches is stubbed.
"""
import json

import pytest


@pytest.fixture()
def frozen_rankings_env(monkeypatch):
    """Make get_rankings() computable with no network and a recorded store/board."""
    import app

    state = {"served": 0, "stored": []}

    stale = {"week": "stale", "season": 2026, "updated": "2026-09-24T21:01:51",
             "teams": [{"name": "Stale", "mascot": "", "conf": "SEC", "emoji": "🏈",
                        "composite": 99.0, "rank": 1, "wins": 3, "losses": 0,
                        "points": 30.0, "movement": 0, "streak": "—"}]}

    def fake_served(kind, year, week=0):
        state["served"] += 1
        return stale

    def fake_enrich(teams, week=None):
        for t in teams:
            t["composite"] = 50.0
        return teams

    monkeypatch.setattr(app, "_board_served", fake_served)
    # get_rankings() reads through _served_analytics() now (D1-first, Phase 6), so stubbing the
    # raw file accessor is no longer enough -- and would let a REAL D1 read into a test that
    # documents itself as hermetic. Stub the accessor get_rankings actually calls.
    monkeypatch.setattr(app, "_served_analytics",
                        lambda: [{"name": "Georgia", "sp_plus": 20.0},
                                 {"name": "Alabama", "sp_plus": 19.0}])
    monkeypatch.setattr(app, "_enrich_with_composite", fake_enrich)
    monkeypatch.setattr(app, "_ap_rank_map", lambda: {})
    monkeypatch.setattr(app, "_coaches_rank_map", lambda: {})
    monkeypatch.setattr(app, "_overlay_records", lambda teams: None)
    monkeypatch.setattr(app, "_rating_vintages", lambda teams: {})
    monkeypatch.setattr(app, "_store_board",
                        lambda kind, payload, year, *a, **k: state["stored"].append(payload) or 1)
    app._rankings_cache.clear()
    return app, state, stale


def test_serve_path_still_uses_the_durable_board(frozen_rankings_env):
    """The request path must keep serving the store — only the REBUILD bypasses it."""
    app, state, stale = frozen_rankings_env
    assert app.get_rankings().teams[0].name == "Stale"
    assert state["served"] == 1


def test_force_bypasses_the_durable_board_and_computes(frozen_rankings_env):
    app, state, stale = frozen_rankings_env
    app._rankings_cache.clear()
    fresh = app.get_rankings(force=True)
    assert fresh.teams[0].name != "Stale"
    assert {t.name for t in fresh.teams} == {"Georgia", "Alabama"}
    assert state["served"] == 0  # the store was never consulted


def test_force_bypasses_the_memory_cache_too(frozen_rankings_env):
    app, state, stale = frozen_rankings_env
    app.get_rankings()                       # warms _rankings_cache with the stale payload
    assert app.get_rankings(force=True).teams[0].name != "Stale"


def test_rebuild_asks_for_a_forced_recompute(monkeypatch):
    """The regression this file exists for."""
    import app

    seen = {}

    class FakeResponse:
        def model_dump(self):
            return {"teams": [{"name": "Fresh", "composite": 1.0}]}

    def fake_get_rankings(force=False):
        seen["force"] = force
        return FakeResponse()

    monkeypatch.setattr(app, "_board_fingerprint", lambda kind, year: "newfp")
    monkeypatch.setattr(app.d1_write_path, "enabled", lambda: True)
    monkeypatch.setattr(app.d1_write_path, "load_slate", lambda *a, **k: {"fingerprint": "oldfp"})
    monkeypatch.setattr(app, "get_rankings", fake_get_rankings)
    monkeypatch.setattr(app, "_store_board", lambda *a, **k: 1)

    out = app._refresh_boards_if_stale((app.BOARD_RANKINGS,))
    assert seen == {"force": True}, "the rebuild read the board it was replacing"
    assert out[app.BOARD_RANKINGS] == "rebuilt"


def test_unchanged_board_is_not_rebuilt(monkeypatch):
    import app

    monkeypatch.setattr(app, "_board_fingerprint", lambda kind, year: "same")
    monkeypatch.setattr(app.d1_write_path, "enabled", lambda: True)
    monkeypatch.setattr(app.d1_write_path, "load_slate", lambda *a, **k: {"fingerprint": "same"})
    monkeypatch.setattr(app, "get_rankings",
                        lambda force=False: pytest.fail("should not rebuild"))
    assert app._refresh_boards_if_stale((app.BOARD_RANKINGS,))[app.BOARD_RANKINGS] == "unchanged"


def test_boards_status_exposes_the_fingerprint_and_its_components(monkeypatch):
    """An EMPTY component must be visible: that is how the fingerprint silently collapses
    back onto the old one and the board freezes while looking healthy."""
    import app

    monkeypatch.setattr(app, "_board_fingerprint", lambda kind, year: "fpA")
    monkeypatch.setattr(app.d1_write_path, "load_slate",
                        lambda *a, **k: {"fingerprint": "fpA", "ts": 123,
                                         "payload": json.dumps(
                                             {"updated": "2026-09-24T21:01:51"})})
    monkeypatch.setattr(app, "_results_signature", lambda year: "abcdef123456" + "0" * 20)
    monkeypatch.setattr(app, "_poll_signature", lambda: "")
    monkeypatch.setattr(app, "_cfbd_season_games",
                        lambda year: [{"completed": True}, {"completed": False}])
    monkeypatch.setattr(app, "_ap_rank_map", lambda: {})
    monkeypatch.setattr(app, "_coaches_rank_map", lambda: {})

    st = app.api_boards_status()
    assert st["rankings"]["match"] is True
    assert st["rankings"]["completed_games"] == 1
    assert st["rankings"]["games_in_feed"] == 2
    assert st["rankings"]["poll_signature"] == ""      # the silent failure, made loud
    assert st["rankings"]["payload_updated"] == "2026-09-24T21:01:51"
    assert st["win_totals"]["results_signature"].startswith("abcdef12")
