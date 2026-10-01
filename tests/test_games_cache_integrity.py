"""Regression: an empty fetch must never destroy the season-games cache.

Found by QA against the served candidate: /api/schedule/weeks and /api/schedule/current-week
returned empty weeks arrays and /api/schedule?week=5 returned zero matchups, while
data/cfbd_season_games.json held 3,679 games.

Cause: `_cfbd_season_games()` fetches; when the fetch is empty it falls back to the disk copy, but
only while that copy is younger than 4x the TTL (2h). The candidate's copy was older than that, so
the fallback was refused -- and the code then wrote the result back UNCONDITIONALLY, putting
`{"ts":..., "games": []}` (39 bytes) over the good cache and destroying the only copy the next boot
could have used. The clobber is what makes the failure permanent; the staleness rule alone would
only have made it empty once.

NOTE: whether a stale-but-real schedule SHOULD be served is a separate product question and is not
decided here -- these tests only assert that the cache is never destroyed.
"""
from __future__ import annotations

import json
import time

import app


STALE = app.CFBD_GAMES_TTL * 10      # well past the 4x fallback cap

GAMES = [{"id": 1, "week": 5, "home": "Georgia"}, {"id": 2, "week": 5, "home": "Ohio State"}]


def _cache(tmp_path, games, ts):
    f = tmp_path / "cfbd_season_games.json"
    f.write_text(json.dumps({"ts": ts, "games": games}), encoding="utf-8")
    return f


def test_empty_fetch_never_overwrites_a_populated_cache(monkeypatch, tmp_path):
    """The regression. Fails on the pre-fix code: the stale cache is clobbered with []."""
    f = _cache(tmp_path, GAMES, time.time() - STALE)
    monkeypatch.setattr(app, "_CFBD_GAMES_FILE", f)
    monkeypatch.setattr(app, "_cfbd_get", lambda *a, **k: [])       # offline / provider hiccup
    monkeypatch.setattr(app, "_CFBD_GAMES_CACHE", {})

    app._cfbd_season_games(app.CFBD_YEAR)

    on_disk = json.loads(f.read_text(encoding="utf-8"))
    assert on_disk["games"] == GAMES, (
        "an empty fetch overwrote the cached slate with [] -- the next boot then has nothing to "
        "fall back to, which is how the served Schedule page went empty with 3,679 games on disk")


def test_a_fresh_cache_is_served_and_survives_an_empty_fetch(monkeypatch, tmp_path):
    f = _cache(tmp_path, GAMES, time.time())
    monkeypatch.setattr(app, "_CFBD_GAMES_FILE", f)
    monkeypatch.setattr(app, "_cfbd_get", lambda *a, **k: [])
    monkeypatch.setattr(app, "_CFBD_GAMES_CACHE", {})

    got = app._cfbd_season_games(app.CFBD_YEAR)

    assert got == GAMES, "a fresh cache should serve the slate when the fetch is empty"
    assert json.loads(f.read_text(encoding="utf-8"))["games"] == GAMES


def test_a_real_fetch_still_writes_the_cache(monkeypatch, tmp_path):
    """The guard must not stop legitimate updates."""
    f = _cache(tmp_path, GAMES, time.time() - STALE)
    fresh = GAMES + [{"id": 3, "week": 6, "home": "Texas"}]
    monkeypatch.setattr(app, "_CFBD_GAMES_FILE", f)
    monkeypatch.setattr(app, "_cfbd_get", lambda *a, **k: fresh)
    monkeypatch.setattr(app, "_CFBD_GAMES_CACHE", {})

    got = app._cfbd_season_games(app.CFBD_YEAR)

    assert got == fresh
    assert json.loads(f.read_text(encoding="utf-8"))["games"] == fresh


def test_no_cache_and_no_fetch_records_the_empty_result(monkeypatch, tmp_path):
    f = tmp_path / "cfbd_season_games.json"
    monkeypatch.setattr(app, "_CFBD_GAMES_FILE", f)
    monkeypatch.setattr(app, "_cfbd_get", lambda *a, **k: [])
    monkeypatch.setattr(app, "_CFBD_GAMES_CACHE", {})

    app._cfbd_season_games(app.CFBD_YEAR)

    assert f.exists(), "with no cache present, an empty result should still be recorded"


# ---------------------------------------------- QA blocker: a PROLONGED outage must not empty the page


def test_a_cache_beyond_the_fallback_limit_is_still_served_and_marked_stale(monkeypatch, tmp_path):
    """QA's release blocker: crossing the fallback limit must not empty the Schedule page.

    The old rule refused a disk copy older than 4x the TTL, so a provider outage longer than that
    served an EMPTY slate. Now the last good copy is served and flagged stale.
    """
    f = _cache(tmp_path, GAMES, time.time() - STALE)
    monkeypatch.setattr(app, "_CFBD_GAMES_FILE", f)
    monkeypatch.setattr(app, "_cfbd_get", lambda *a, **k: [])
    monkeypatch.setattr(app, "_CFBD_GAMES_CACHE", {})
    monkeypatch.setattr(app, "_CFBD_GAMES_STATE", {})

    got = app._cfbd_season_games(app.CFBD_YEAR)

    assert got == GAMES, "a prolonged outage must still serve the last good slate, not nothing"
    fr = app._cfbd_games_freshness(app.CFBD_YEAR)
    assert fr["stale"] is True, "an old slate must be flagged, not served silently as fresh"
    assert "stale" in fr["data_source"]
    assert fr["data_age_hours"] and fr["data_age_hours"] > app.CFBD_GAMES_TTL * 4 / 3600

    # Serving from disk must NOT re-stamp the file: that would launder the age we just reported.
    assert json.loads(f.read_text(encoding="utf-8"))["ts"] < time.time() - STALE * 0.9, (
        "the cache age was reset by a read -- the staleness indication would then be a lie")


def test_a_fresh_cache_is_not_flagged_stale(monkeypatch, tmp_path):
    f = _cache(tmp_path, GAMES, time.time())
    monkeypatch.setattr(app, "_CFBD_GAMES_FILE", f)
    monkeypatch.setattr(app, "_cfbd_get", lambda *a, **k: [])
    monkeypatch.setattr(app, "_CFBD_GAMES_CACHE", {})
    monkeypatch.setattr(app, "_CFBD_GAMES_STATE", {})

    app._cfbd_season_games(app.CFBD_YEAR)

    fr = app._cfbd_games_freshness(app.CFBD_YEAR)
    assert fr["stale"] is False, "a copy inside the window must not be reported as stale"
    assert fr["data_source"] == "disk"


def test_a_live_fetch_reports_live(monkeypatch, tmp_path):
    f = _cache(tmp_path, GAMES, time.time() - STALE)
    monkeypatch.setattr(app, "_CFBD_GAMES_FILE", f)
    monkeypatch.setattr(app, "_cfbd_get", lambda *a, **k: GAMES)
    monkeypatch.setattr(app, "_CFBD_GAMES_CACHE", {})
    monkeypatch.setattr(app, "_CFBD_GAMES_STATE", {})

    app._cfbd_season_games(app.CFBD_YEAR)

    fr = app._cfbd_games_freshness(app.CFBD_YEAR)
    assert fr["stale"] is False and fr["data_source"] == "live"