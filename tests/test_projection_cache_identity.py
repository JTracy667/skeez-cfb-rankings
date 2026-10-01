"""QA remediation §6 — the projections cache identity must describe its real inputs.

Two defects:
  1. The disk-fallback identity was the constant string "disk", so a CHANGED fallback file
     produced the same cache key and the stale payload kept being served.
  2. An empty (or degraded) result was cached as fresh, pinning a bad answer in place.

No network: the D1 entry points and the served-analytics call are monkeypatched.
"""
from __future__ import annotations

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


def test_degraded_serve_state_is_part_of_the_fallback_key(monkeypatch, tmp_path):
    f = tmp_path / "cfbd_analytics.json"
    f.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(app, "_CFBD_ANALYTICS_FILE", f)
    monkeypatch.setattr(app.d1_write_path, "analytics_publication", lambda season: None)

    app._set_serve_state("disk (no verified publication)", True, "no marker")
    degraded = app._projections_cache_key(5)
    app._set_serve_state("d1", False, "")
    verified = app._projections_cache_key(5)

    assert degraded != verified, "a degraded serve must not share a key with a verified one"


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


def test_a_degraded_result_is_not_cached_as_fresh(monkeypatch):
    monkeypatch.setattr(app, "live_week", lambda: 5)
    monkeypatch.setattr(app, "_served_analytics",
                        lambda: [{"name": "Georgia", "sp_plus": 30.2, "conf": "SEC",
                                  "mascot": "Bulldogs"}])
    monkeypatch.setattr(app.d1_write_path, "analytics_publication", lambda season: None)
    app._set_serve_state("disk (no verified publication)", True, "no marker")
    app._proj_cache["key"], app._proj_cache["body"] = None, None

    app.api_projections()

    assert app._proj_cache["body"] is None, \
        "a payload computed from a degraded serve must not be cached as fresh"


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