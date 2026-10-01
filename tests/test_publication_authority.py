"""QA remediation §2 (publication authority) and §5 (exact replay verification).

§2: the publication marker names ONE pull as the season's complete snapshot. Before this,
    ANY call to snapshot_team_analytics could publish, so a delegated probe that invoked the
    weekly pull with a one-team payload named itself the season's complete snapshot and every
    reader then served that stub. Publication now requires BOTH an explicit authoritative-pull
    declaration AND a manifest that meets the season-scale floor, and the pull's per-key
    (count, sum) must match the read-back exactly.
§5: a replay of an already-landed chunk was "satisfied" on row counts, so a replay whose
    values had changed was accepted while the database kept the old value (`DO NOTHING`).

These run on the hermetic SQLite harness: no credentials, no production writes. D1-specific
behaviour (REST error text, `meta` accounting, request limits) is NOT proven here.
"""
from __future__ import annotations

import os

import pytest

import d1_harness as H
import d1_store
import d1_write_path as dw

os.environ.setdefault("D1_WRITE_ENABLED", "1")

SEASON, WEEK = 2026, 5
T1 = "2026-09-30T04:00:00+00:00"

# The real floors (not lowered anywhere in this file): 600 teams / 40 keys.
FULL_TEAMS = 600
FULL_KEYS = [f"metric_{i:02d}" for i in range(40)]


@pytest.fixture
def conn(monkeypatch):
    c = H.open_sqlite("append")
    H.patch_d1(monkeypatch, c)
    return c


def _seed(c, teams):
    c.executemany("INSERT INTO teams (team_id, name) VALUES (?, ?)",
                  [(t["team_id"], t["name"]) for t in teams])
    c.commit()


def _full_teams(n=FULL_TEAMS):
    """A season-scale fixture: every team carries every key, so the manifest is complete."""
    out = []
    for i in range(n):
        t = {"team_id": 1000 + i, "name": f"Team {i}"}
        for j, k in enumerate(FULL_KEYS):
            t[k] = 10.0 + (i % 31) + j / 100.0
        out.append(t)
    return out


# ── §2 ────────────────────────────────────────────────────────────────────────────────────
def test_one_team_pull_cannot_name_itself_the_season_snapshot(conn):
    """The reported defect, at its smallest: a probe's one-team payload."""
    solo = [{"team_id": 1, "name": "Solo", "sp_plus": 30.2}]
    _seed(conn, solo)

    n = dw.snapshot_team_analytics(solo, ["sp_plus"], SEASON, WEEK, authoritative_pull=True)

    assert n == 1, "the rows still land -- archiving a partial pull is harmless"
    assert dw.analytics_publication(SEASON) is None, "a partial pull must NOT publish a marker"


def test_a_complete_pull_that_is_not_declared_authoritative_cannot_publish(conn):
    """The floor is not the only gate: publication is an explicit act by the coordinator."""
    teams = _full_teams()
    _seed(conn, teams)

    dw.snapshot_team_analytics(teams, FULL_KEYS, SEASON, WEEK)   # publish=True by default

    assert dw.analytics_publication(SEASON) is None, \
        "only the authoritative full-pull coordinator may publish"


def test_the_coordinator_publishes_and_the_marker_records_its_manifest(conn):
    teams = _full_teams()
    _seed(conn, teams)

    dw.snapshot_team_analytics(teams, FULL_KEYS, SEASON, WEEK, authoritative_pull=True)
    pub = dw.analytics_publication(SEASON)

    assert pub is not None, "a complete, declared, verified pull must publish"
    assert pub["n_teams"] == FULL_TEAMS
    assert pub["n_rows"] == FULL_TEAMS * len(FULL_KEYS)
    assert pub["keys"] == sorted(FULL_KEYS), "the marker carries the key set it claims"
    assert pub["n_keys"] == len(FULL_KEYS)


def test_publication_is_refused_when_the_readback_does_not_match(conn, monkeypatch):
    """Counts are not a manifest: a per-key mismatch must refuse the publication."""
    teams = _full_teams()
    _seed(conn, teams)
    real = dw._read_back_manifest
    tampered = {"n": 0}

    def _drifted(season, week, stamp):
        man = dict(real(season, week, stamp))
        if not tampered["n"]:
            k = sorted(man)[0]
            n, s = man[k]
            man[k] = (n, s + 5.0)      # one key's values are not what the pull wrote
            tampered["n"] += 1
        return man

    monkeypatch.setattr(dw, "_read_back_manifest", _drifted)

    dw.snapshot_team_analytics(teams, FULL_KEYS, SEASON, WEEK, authoritative_pull=True)

    assert dw.analytics_publication(SEASON) is None, \
        "a read-back that disagrees with the pull must not be published"


# ── §5 ────────────────────────────────────────────────────────────────────────────────────
def test_an_exact_replay_is_recognised_as_present(conn):
    teams = [{"team_id": 1, "name": "Alpha", "sp_plus": 30.2}]
    _seed(conn, teams)
    rows = dw.team_analytics_rows(teams, ["sp_plus"], SEASON, WEEK)
    for r in rows:
        r["recorded_at"] = T1
    d1_store.upsert_stat_observations_bulk(rows)

    assert d1_store._chunk_rows_present(rows) is True


def test_a_replay_whose_value_changed_is_not_present(conn):
    """`DO NOTHING` keeps the old value, so counting this as satisfied hides the divergence."""
    teams = [{"team_id": 1, "name": "Alpha", "sp_plus": 30.2}]
    _seed(conn, teams)
    rows = dw.team_analytics_rows(teams, ["sp_plus"], SEASON, WEEK)
    for r in rows:
        r["recorded_at"] = T1
    d1_store.upsert_stat_observations_bulk(rows)

    changed = [dict(r, value=float(r["value"]) + 1.0) for r in rows]
    assert d1_store._chunk_rows_present(changed) is False


def test_a_replay_missing_a_row_is_not_present(conn):
    teams = [{"team_id": 1, "name": "Alpha", "sp_plus": 30.2, "talent": 0.88}]
    _seed(conn, teams)
    rows = dw.team_analytics_rows(teams, ["sp_plus", "talent"], SEASON, WEEK)
    for r in rows:
        r["recorded_at"] = T1
    d1_store.upsert_stat_observations_bulk(rows[:1])

    assert d1_store._chunk_rows_present(rows) is False