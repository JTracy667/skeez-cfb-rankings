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
    real = dw._read_back_rows
    tampered = {"n": 0}

    def _drifted(season, week, stamp, expected, page=1000):
        rows = [dict(r) for r in real(season, week, stamp, expected, page)]
        if not tampered["n"]:
            tampered["n"] += 1
            rows[0] = {**rows[0], "value": float(rows[0]["value"]) + 5.0}
        return rows

    monkeypatch.setattr(dw, "_read_back_rows", _drifted)

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

# ── QA counterexample 4: the manifest must be the ROW SET, not counts/sums ────────────────
def test_an_offsetting_pair_of_value_changes_is_caught(conn, monkeypatch):
    """Two teams offset by +5 and -5: every per-key COUNT and SUM is unchanged.

    This is QA's independent probe. The old per-key (count, sum) manifest passed it and the
    wrong values were published; the exact row-set read-back must refuse.
    """
    teams = _full_teams()
    _seed(conn, teams)
    key = FULL_KEYS[0]

    def _sums():
        return {r["stat_key"]: (r["n"], r["s"]) for r in conn.execute(
            "SELECT stat_key, COUNT(*) AS n, SUM(value) AS s FROM stat_observations "
            "WHERE season = ? AND week = ? AND subject_type = 'team' GROUP BY stat_key",
            (SEASON, WEEK)).fetchall()}

    real_q = d1_store.query
    tampered = {"done": False}

    def _q(sql, params=None, timeout=60):
        if "ORDER BY subject_id, stat_key" in sql and not tampered["done"]:
            tampered["done"] = True
            conn.execute("UPDATE stat_observations SET value = value + 5 WHERE subject_id = 1000 "
                         "AND stat_key = ?", (key,))
            conn.execute("UPDATE stat_observations SET value = value - 5 WHERE subject_id = 1001 "
                         "AND stat_key = ?", (key,))
            conn.commit()
            before, after = _sums(), None
            conn.execute("UPDATE stat_observations SET value = value WHERE 0")  # no-op
            after = _sums()
            assert before == after, "the probe must leave counts and sums identical"
        return real_q(sql, params, timeout)

    monkeypatch.setattr(d1_store, "query", _q)

    dw.snapshot_team_analytics(teams, FULL_KEYS, SEASON, WEEK, authoritative_pull=True)

    assert dw.analytics_publication(SEASON) is None, \
        "values that disagree with the pull must not be published, sums notwithstanding"


def test_an_extra_row_at_the_stamp_blocks_publication(conn, monkeypatch):
    """A row at the stamp that the pull did not write must be rejected, not adopted."""
    teams = _full_teams()
    _seed(conn, teams)
    real = dw._read_back_rows

    def _with_extra(season, week, stamp, expected, page=1000):
        return real(season, week, stamp, expected, page) + [
            {"subject_id": 999999, "stat_key": "intruder_key", "value": 1.0}]

    monkeypatch.setattr(dw, "_read_back_rows", _with_extra)

    dw.snapshot_team_analytics(teams, FULL_KEYS, SEASON, WEEK, authoritative_pull=True)

    assert dw.analytics_publication(SEASON) is None, \
        "an extra row at the stamp means the read-back is not this pull"