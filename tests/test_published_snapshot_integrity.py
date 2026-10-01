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