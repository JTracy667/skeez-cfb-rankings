"""QA remediation §6 — the projections cache identity must describe its real inputs.

Two defects:
  1. The disk-fallback identity was the constant string "disk", so a CHANGED fallback file
     produced the same cache key and the stale payload kept being served.
  2. An empty (or degraded) result was cached as fresh, pinning a bad answer in place.

No network: the D1 entry points and the served-analytics call are monkeypatched.
"""
from __future__ import annotations

import pytest

import app


def _pub(stamp="2026-09-30T16:18:36Z", rows=34552, keys=115):
    return {"stamp": stamp, "week": 5, "n_rows": rows, "n_keys": keys,
            "identity": {"Georgia": {}}}


def test_disk_identity_tracks_the_file_it_actually_read(monkeypatch, tmp_path):
    f = tmp_path / "cfbd_analytics.json"
    f.write_text('{"a": 1}', encoding="utf-8")
    monkeypatch.setattr(app, "_CFBD_ANALYTICS_FILE", f)
    monkeypatch.setattr(app.d1_write_path, "analytics_publication", lambda season: None)

    first = app._projections_cache_key(5)

    f.write_text('{"a": 1, "b": 2, "c": 3}', encoding="utf-8")   # different content
    second = app._projections_cache_key(5)

    assert first != second, "a changed fallback file must not reuse the cached payload"


def test_missing_fallback_file_is_its_own_identity(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "_CFBD_ANALYTICS_FILE", tmp_path / "nope.json")
    monkeypatch.setattr(app.d1_write_path, "analytics_publication", lambda season: None)
    assert "disk:absent" in str(app._projections_cache_key(5))


def test_the_fallback_key_is_stable_while_serve_state_changes(monkeypatch, tmp_path):
    """The key must not depend on state that serving itself mutates.

    An earlier version put `_ANALYTICS_SERVE` in the key, so the first request keyed on
    "unknown" and its own warm follow-up keyed on "disk (no verified publication)" -- every
    hit missed and the payload was recomputed on every request.
    """
    f = tmp_path / "cfbd_analytics.json"
    f.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(app, "_CFBD_ANALYTICS_FILE", f)
    monkeypatch.setattr(app.d1_write_path, "analytics_publication", lambda season: None)
    monkeypatch.setattr(app, "_analytics_identity_map", lambda pub=None: {})

    app._set_serve_state("unknown", False, "")
    before_serving = app._projections_cache_key(5)
    app._set_serve_state("disk (no verified publication)", True, "no marker")
    after_serving = app._projections_cache_key(5)

    assert before_serving == after_serving, "a warm hit must still hit"


@pytest.mark.needs_d1
def test_a_published_pull_keys_on_the_pull_not_the_disk(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "_CFBD_ANALYTICS_FILE", tmp_path / "cfbd_analytics.json")
    monkeypatch.setattr(app.d1_write_path, "analytics_publication", lambda season: _pub())
    a = app._projections_cache_key(5)
    monkeypatch.setattr(app.d1_write_path, "analytics_publication",
                        lambda season: _pub(stamp="2026-10-07T16:00:00Z"))
    b = app._projections_cache_key(5)
    assert a != b, "a new published pull must invalidate the cached payload"


def test_an_empty_projection_set_is_not_cached_as_fresh(monkeypatch):
    monkeypatch.setattr(app, "live_week", lambda: 5)
    monkeypatch.setattr(app, "_served_analytics", lambda: [])
    monkeypatch.setattr(app.d1_write_path, "analytics_publication", lambda season: None)

    class _R:
        teams: list = []

    monkeypatch.setattr(app, "get_rankings", lambda: _R())
    app._proj_cache["key"], app._proj_cache["body"] = None, None

    resp = app.api_projections()

    assert resp is not None
    assert app._proj_cache["body"] is None, "an empty result must not be cached as fresh"


@pytest.mark.needs_d1
def test_a_fallback_payload_is_not_reused_once_a_publication_appears(monkeypatch, tmp_path):
    """A payload built from the fallback must never satisfy a request that has a publication.

    The store decision is INPUT-based, not `_ANALYTICS_SERVE`-based: that global is set by
    whichever request served last, so gating the cache on it made a warm follow-up depend on
    unrelated state (and putting it in the key made every warm hit miss, because serving SETS
    it). Which door produced a payload is carried by the key itself instead.
    """
    monkeypatch.setattr(app, "_CFBD_ANALYTICS_FILE", tmp_path / "cfbd_analytics.json")
    monkeypatch.setattr(app, "_analytics_identity_map", lambda pub=None: {"G": {}})
    monkeypatch.setattr(app.d1_write_path, "analytics_publication", lambda season: None)
    app._set_serve_state("disk (no verified publication)", True, "no marker")
    fallback_key = app._projections_cache_key(5)

    monkeypatch.setattr(app.d1_write_path, "analytics_publication", lambda season: _pub())
    app._set_serve_state("d1", False, "")
    verified_key = app._projections_cache_key(5)

    assert fallback_key != verified_key, (
        "a fallback payload must not be reused once a verified publication exists")


@pytest.mark.needs_d1
def test_a_verified_result_is_cached(monkeypatch):
    monkeypatch.setattr(app, "live_week", lambda: 5)
    monkeypatch.setattr(app, "_served_analytics",
                        lambda: [{"name": "Georgia", "sp_plus": 30.2, "conf": "SEC",
                                  "mascot": "Bulldogs"}])
    monkeypatch.setattr(app.d1_write_path, "analytics_publication", lambda season: _pub())
    app._set_serve_state("d1", False, "")
    app._proj_cache["key"], app._proj_cache["body"] = None, None

    app.api_projections()

    assert app._proj_cache["body"] is not None, "a verified result should be cached"
    assert app._proj_cache["key"][1][0] == "pull"

# ── QA counterexample 3: the cache must key on the identity it serves ─────────────────────
@pytest.mark.needs_d1
def test_a_served_team_rename_invalidates_the_cached_payload(monkeypatch, tmp_path):
    """OLD -> NEW under the same marker returned OLD: the identity was not in the key."""
    monkeypatch.setattr(app, "_CFBD_ANALYTICS_FILE", tmp_path / "cfbd_analytics.json")
    monkeypatch.setattr(app.d1_write_path, "analytics_publication", lambda season: _pub())
    ident = {"OLD": {"conf": "SEC"}}
    monkeypatch.setattr(app, "_analytics_identity_map", lambda pub=None: ident)

    first = app._projections_cache_key(5)

    ident.clear()
    ident["NEW"] = {"conf": "SEC"}
    second = app._projections_cache_key(5)

    assert first != second, "a served rename must invalidate the cached projections payload"


@pytest.mark.needs_d1
def test_a_rename_makes_the_endpoint_recompute(monkeypatch):
    """End to end: the rename must reach the response, and serving must run again."""
    calls = {"n": 0}

    def _served():
        calls["n"] += 1
        return [{"name": "OLD" if calls["n"] == 1 else "NEW", "sp_plus": 30.2, "conf": "SEC"}]

    monkeypatch.setattr(app, "live_week", lambda: 5)
    monkeypatch.setattr(app, "_served_analytics", _served)
    monkeypatch.setattr(app.d1_write_path, "analytics_publication", lambda season: _pub())
    ident = {"OLD": {}}
    monkeypatch.setattr(app, "_analytics_identity_map", lambda pub=None: ident)
    app._set_serve_state("d1", False, "")
    app._proj_cache["key"], app._proj_cache["body"] = None, None

    first = app.api_projections()
    assert b"OLD" in first.body

    ident.clear()
    ident["NEW"] = {}
    second = app.api_projections()

    assert b"NEW" in second.body, "the rename must reach the response"
    assert calls["n"] == 2, "the serving function must run again instead of serving the cache"