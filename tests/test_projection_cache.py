"""Task 2 (projections cache) + Task 3 (bounded D1 reads on the serving path).

QA rulings Q3/Q7: the cache key must be the PUBLISHED identity (exact cross-process
invalidation), errors/empties must never be cached as fresh, disk-fallback is a DISTINCT
identity, and the response must be byte-identical for a given key. Task 3: instrument the D1
client and assert a bounded read count instead of trusting the shape.

Run: python -m pytest tests/test_projection_cache.py -v
"""
from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("CFB_SKIP_BOOTWARM", "1")
os.environ.setdefault("REFRESH_INTERVAL_SECONDS", "0")
os.environ.setdefault("D1_WRITE_ENABLED", "1")

TEAMS = [{"name": f"Team{i:03d}", "team_id": i, "sp_plus": 30.0 - i * 0.01,
          "efficiency": 0.5, "talent": 0.6, "experience": 0.5, "conf": "FBS",
          "mascot": "", "emoji": "\U0001F3C8"} for i in range(1, 41)]


@pytest.fixture()
def app_env(monkeypatch):
    """A deterministic app: fixed served teams, fixed publication record, counting compute."""
    import app  # noqa: PLC0415

    state = {"compute": 0, "stamp": "2026-09-30T16:18:36.601938+00:00"}
    monkeypatch.setattr(app, "_served_analytics", lambda: [dict(t) for t in TEAMS])
    monkeypatch.setattr(app.d1_write_path, "analytics_publication",
                        lambda season: {"season": season, "week": 5, "stamp": state["stamp"],
                                        "n_rows": 1, "n_teams": len(TEAMS), "identity": {}})

    real = app.project_score_multi_factor

    def counting(*a, **k):
        state["compute"] += 1
        return real(*a, **k)

    monkeypatch.setattr(app, "project_score_multi_factor", counting)
    app._proj_cache["key"] = None
    app._proj_cache["body"] = None
    return app, state


def _client(app):
    from fastapi.testclient import TestClient  # noqa: PLC0415
    return TestClient(app.app)


@pytest.mark.needs_d1
def test_warm_request_does_not_recompute_and_is_byte_identical(app_env):
    app, state = app_env
    c = _client(app)
    first = c.get("/api/projections")
    assert first.status_code == 200
    computed_after_first = state["compute"]
    assert computed_after_first == len(TEAMS) * 2, "both directions computed once"

    second = c.get("/api/projections")
    assert second.status_code == 200
    assert state["compute"] == computed_after_first, "a warm hit must not recompute"
    assert second.content == first.content, "same key -> byte-identical payload"


@pytest.mark.needs_d1
def test_new_published_stamp_invalidates_across_processes(app_env):
    """The key is the publication identity, so another container's pull invalidates it."""
    app, state = app_env
    c = _client(app)
    c.get("/api/projections")
    n = state["compute"]
    state["stamp"] = "2026-09-30T17:00:00+00:00"      # a new complete pull landed elsewhere
    c.get("/api/projections")
    assert state["compute"] > n, "a new stamp must recompute"


def test_disk_fallback_is_a_distinct_cache_identity(app_env):
    app, state = app_env
    c = _client(app)
    c.get("/api/projections")
    n = state["compute"]
    app.d1_write_path.analytics_publication = lambda season: None   # no record -> disk
    c.get("/api/projections")
    assert state["compute"] > n, "fallback inputs must not reuse the published key"


@pytest.mark.needs_d1
def test_payload_shape_and_ordering_are_unchanged(app_env):
    """Exact parity: the endpoint's payload is what the pre-change code produced."""
    app, _state = app_env
    c = _client(app)
    got = json.loads(c.get("/api/projections").content)
    assert got["count"] == len(TEAMS)
    assert set(got.keys()) == {"projections", "count"}
    comps = [p["home_projection"]["composite"] for p in got["projections"]]
    assert comps == sorted(comps, reverse=True), "ordering preserved (composite desc)"
    names = [p["name"] for p in got["projections"]]
    assert sorted(names) == sorted(t["name"] for t in TEAMS), "team universe preserved"
    for p in got["projections"]:
        assert set(p.keys()) >= {"home_projection", "away_projection", "name", "team_id"}


@pytest.mark.needs_d1
def test_a_failed_compute_is_never_cached_as_fresh(app_env):
    app, state = app_env
    c = _client(app)
    boom = {"n": 0}

    def failing(*a, **k):
        boom["n"] += 1
        raise RuntimeError("projection blew up")

    app.project_score_multi_factor = failing
    r = c.get("/api/projections")
    assert r.status_code == 200 and "error" in r.json()
    assert app._proj_cache["body"] is None, "an error must not become a fresh cache entry"

    app.project_score_multi_factor = lambda *a, **k: {"composite": 1.0}
    r2 = c.get("/api/projections")
    assert r2.status_code == 200 and "error" not in r2.json(), "retry must still work"


def test_serving_path_uses_a_bounded_number_of_d1_reads(monkeypatch):
    """Task 3: instrument the D1 boundary and assert the read count, not the shape."""
    import app  # noqa: PLC0415
    import d1_store  # noqa: PLC0415

    calls = {"n": 0, "marker": 0}
    real_query = d1_store.query

    def counting(sql, params=None, timeout=60):
        calls["n"] += 1
        return real_query(sql, params, timeout)

    monkeypatch.setattr(d1_store, "query", counting)
    monkeypatch.setattr(
        app.d1_write_path, "analytics_publication",
        lambda season: (calls.__setitem__("marker", calls["marker"] + 1)
                        or {"season": season, "week": 5, "stamp": "S1", "n_rows": 1,
                            "n_teams": 1, "identity": {}}))

    monkeypatch.setattr(app.d1_write_path, "_TEAM_IDENTITY_CACHE", {"at": 0.0, "rows": None})

    app._served_analytics()
    assert calls["marker"] == 1, f"one marker read per request, got {calls['marker']}"
    assert calls["n"] <= 4, f"serving path read D1 {calls['n']} times; expected <= 4"


def test_analytics_opt_out_performs_zero_d1_reads(monkeypatch):
    """ANALYTICS_FROM_D1=0 must not touch D1 at all (the identity read used to run first)."""
    import app  # noqa: PLC0415
    import d1_store  # noqa: PLC0415

    calls = {"n": 0}

    def counting(*a, **k):
        calls["n"] += 1
        raise AssertionError("D1 must not be read on the opt-out path")

    monkeypatch.setenv("ANALYTICS_FROM_D1", "0")
    monkeypatch.setattr(d1_store, "query", counting)
    monkeypatch.setattr(d1_store, "query_full", counting)
    app._served_analytics()
    assert calls["n"] == 0