"""QA round 3 — the READER must verify the published VALUES, not just counts.

QA's probe: publish 24,000 rows, then change two values under the SAME stamp (10/10 -> 15/5).
`load_team_analytics()` served 15/5 while the marker stayed unchanged, because the reader
checked only row and team counts (d1_write_path.py:553-559). Write-time verification cannot
catch drift that happens afterwards.

An offsetting pair is used deliberately: it leaves the row count, the team count AND every
per-key sum identical, so nothing short of comparing the values can detect it.

Hermetic SQLite harness: no credentials, no production writes.
"""
from __future__ import annotations

import os

import pytest

import app
import d1_harness as H
import d1_write_path as dw

os.environ.setdefault("D1_WRITE_ENABLED", "1")

SEASON, WEEK = 2026, 5
FULL_TEAMS = 600
FULL_KEYS = [f"metric_{i:02d}" for i in range(40)]


@pytest.fixture
def conn(monkeypatch):
    c = H.open_sqlite("append")
    H.patch_d1(monkeypatch, c)
    return c


def _full_teams(n=FULL_TEAMS):
    out = []
    for i in range(n):
        t = {"team_id": 1000 + i, "name": f"Team {i}"}
        for j, k in enumerate(FULL_KEYS):
            t[k] = 10.0 + (i % 31) + j / 100.0
        out.append(t)
    return out


def _publish(conn):
    teams = _full_teams()
    conn.executemany("INSERT INTO teams (team_id, name) VALUES (?, ?)",
                     [(t["team_id"], t["name"]) for t in teams])
    conn.commit()
    dw.snapshot_team_analytics(teams, FULL_KEYS, SEASON, WEEK, authoritative_pull=True)
    pub = dw.analytics_publication(SEASON)
    assert pub is not None, "the fixture must publish"
    return pub


def _mutate_under_the_stamp(conn, stamp, key):
    """10/10 -> 15/5: counts, team counts and per-key sums are all unchanged."""
    conn.execute("UPDATE stat_observations SET value = 15.0 WHERE subject_id = 1000 "
                 "AND stat_key = ? AND recorded_at = ?", (key, stamp))
    conn.execute("UPDATE stat_observations SET value = 5.0 WHERE subject_id = 1001 "
                 "AND stat_key = ? AND recorded_at = ?", (key, stamp))
    conn.commit()


def test_a_published_snapshot_is_served_when_untouched(conn):
    """Positive control: the check must not refuse everything."""
    _publish(conn)
    served = dw.load_team_analytics(SEASON)
    assert served, "an untouched published snapshot must serve"
    assert len(served) == FULL_TEAMS


def test_a_value_changed_after_publication_is_not_served(conn):
    """QA's regression: the marker is intact and the counts agree, but the VALUES moved."""
    pub = _publish(conn)
    key = FULL_KEYS[0]
    _mutate_under_the_stamp(conn, pub["stamp"], key)

    # prove the old check was blind to this: counts and sums are unchanged
    n_rows, n_teams = dw._selection_counts(SEASON, WEEK, pub["stamp"])
    assert (n_rows, n_teams) == (pub["n_rows"], pub["n_teams"]), \
        "the mutation must leave the counts the reader used to trust identical"

    assert dw.load_team_analytics(SEASON) == [], \
        "an altered snapshot must not be served just because its counts agree"


def test_the_serve_path_reports_the_failure_and_falls_back(conn, monkeypatch):
    """End to end: the altered snapshot never reaches a visitor."""
    pub = _publish(conn)
    _mutate_under_the_stamp(conn, pub["stamp"], FULL_KEYS[0])

    monkeypatch.setattr(app, "_load_cfbd_analytics_file",
                        lambda: [{"name": "Fallback Team", "sp_plus": 1.0, "conf": "FBS"}])
    monkeypatch.setattr(app, "_analytics_identity_map", lambda pub=None: {})

    out = app._served_analytics()

    assert out and out[0]["name"] == "Fallback Team", "the documented fallback serves instead"
    assert not any(abs(float(t.get("sp_plus") or 0.0) - 15.0) < 1e-9 for t in out), \
        "the altered value must not reach the response"
    assert app._ANALYTICS_SERVE["degraded"] is True, "a fallback serve must say it is degraded"


def test_a_marker_without_a_digest_serves_but_is_never_called_verified(conn, monkeypatch):
    """Legacy markers predate the digest: serve them, and label them unverified."""
    pub = _publish(conn)
    legacy = dict(pub)
    legacy.pop("digest", None)
    dw.d1_store.set_app_state(dw.publication_key(SEASON), __import__("json").dumps(legacy))

    assert dw.load_team_analytics(SEASON), "a legacy marker still serves (no blackout)"

    monkeypatch.setattr(app, "_load_cfbd_analytics_file", lambda: [])
    monkeypatch.setattr(app, "_analytics_identity_map", lambda pub=None: {})
    app._served_analytics()
    assert app._ANALYTICS_SERVE["degraded"] is True, \
        "a marker that cannot be verified must not be reported as verified"

class _FallbackTeam:
    def __init__(self, d):
        self._d = d

    def model_dump(self):
        return dict(self._d)


class _FallbackRankings:
    def __init__(self, teams):
        self.teams = teams


def test_a_warm_projection_hit_cannot_serve_a_drifted_snapshot(conn, monkeypatch):
    """QA round 4: the reader refuses the drifted snapshot, but a WARM HIT went around it.

    Sequence: publish -> `api_projections()` caches a payload -> two values change under the
    same stamp -> `api_projections()` again. The cache key carried the marker's metadata and
    the served identity but nothing derived from the ROW VALUES, so the key was unchanged: the
    warm hit returned the pre-drift payload while the serve state still said d1/degraded false.
    A reader check that a cache can skip is not a check.
    """
    import json as _json

    pub = _publish(conn)
    monkeypatch.setattr(app, "live_week", lambda: WEEK)
    monkeypatch.setattr(app, "project_score_multi_factor",
                        lambda td, is_home=True, week=None: {"composite": 1.0, "total": 40.0})
    monkeypatch.setattr(app, "get_rankings",
                        lambda: _FallbackRankings([_FallbackTeam({"name": "Fallback Team"})]))
    app._proj_cache["key"], app._proj_cache["body"] = None, None

    first = _json.loads(app.api_projections().body)
    assert first["count"] >= FULL_TEAMS, "the fixture must cache a full payload"
    cached_body = app._proj_cache["body"]
    assert cached_body is not None, "the first call must populate the cache"

    _mutate_under_the_stamp(conn, pub["stamp"], FULL_KEYS[0])

    second_body = app.api_projections().body

    assert second_body != cached_body, (
        "a warm hit must not return the pre-drift payload after the stored values changed")
    assert app._ANALYTICS_SERVE["degraded"] is True, (
        "post-publication drift must surface as a degraded serve, not a confident d1 serve")
    second = _json.loads(second_body)
    # Assert on the VALUES, not on the team names: identity (who a team is) legitimately comes
    # from the teams table and survives; it is the analytics VALUES that must not be served.
    drifted = [t.get("metric_00") for t in second["projections"]
               if t.get("metric_00") in (15.0, 5.0)]
    assert not drifted, (
        f"the drifted values must not reach visitors, warm hit or not: {drifted}")
