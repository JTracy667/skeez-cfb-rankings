"""Task 4 + 9 — append-only snapshot selection must be driven by a COMPLETE pull.

RED-THEN-GREEN (QA ruling Q1/Q2, 2026-09-30). These run on the hermetic SQLite harness
(`tests/d1_harness.py`); no credentials, no production writes. D1-specific behaviour
(REST error text, meta accounting, request limits) is NOT proven here and stays
UNVERIFIABLE until a scratch D1 is authorized.

The defect these pin down (verified live code, v62):
    d1_write_path.load_team_analytics() selects `recorded_at = MAX(recorded_at)` for the
    (season, week, subject_type='team') slice. The archive is APPEND-ONLY, so a LATER
    single-row writer -- fcs_poll (SEASONS includes 2026), massey_fcs.py,
    backfill_unmatched_names.py, backfill_d1.py -- becomes the entire selected numeric
    payload for that week: every other metric disappears and the composite silently
    imputes 50 for the missing inputs.
"""
from __future__ import annotations

import os

import pytest

import d1_harness as H

# The write gate is env-driven (d1_write_path.write_enabled()); set it here so the tests
# do not depend on another module's env setup.
os.environ.setdefault("D1_WRITE_ENABLED", "1")
os.environ.setdefault("CFB_SKIP_BOOTWARM", "1")
os.environ.setdefault("REFRESH_INTERVAL_SECONDS", "0")

SEASON, WEEK = 2026, 5

@pytest.fixture(autouse=True)
def _small_fixture_floors(monkeypatch):
    """This file's fixture is 3 teams wide, so the §2 completeness FLOOR is lowered here.

    The floor itself -- and the refusal of a partial pull -- is asserted with the REAL
    thresholds in tests/test_publication_authority.py. These tests are about selection
    and publication atomicity, not about the floor.
    """
    import d1_write_path as _dw
    monkeypatch.setattr(_dw, "MIN_PUBLISH_TEAMS", 1, raising=False)
    monkeypatch.setattr(_dw, "MIN_PUBLISH_KEYS", 1, raising=False)

KEYS = ["sp_plus", "efficiency", "talent"]
T1 = "2026-09-30T04:00:00+00:00"
T2 = "2026-09-30T09:00:00+00:00"   # the later, partial writer

TEAMS = [{"team_id": 1, "name": "Alpha", "sp_plus": 30.2, "efficiency": 0.71, "talent": 0.88},
         {"team_id": 2, "name": "Bravo", "sp_plus": 22.4, "efficiency": 0.55, "talent": 0.61},
         {"team_id": 3, "name": "Delta", "sp_plus": 14.9, "efficiency": 0.41, "talent": 0.35}]


def _seed_teams(conn):
    conn.executemany("INSERT INTO teams (team_id, name) VALUES (?, ?)",
                     [(t["team_id"], t["name"]) for t in TEAMS])
    conn.commit()


@pytest.fixture()
def d1(monkeypatch):
    """(d1_write_path, d1_store, conn, stats) on a fresh append-only database."""
    conn = H.open_sqlite("append")
    _seed_teams(conn)
    stats = H.patch_d1(monkeypatch, conn)
    import d1_store
    import d1_write_path
    return d1_write_path, d1_store, conn, stats


def _next_stamp(conn) -> str:
    """A stamp guaranteed LATER than everything already in the table (the bulk writer
    stamps the real current time, so a hardcoded literal is not reliably later)."""
    from datetime import datetime, timedelta, timezone
    cur = conn.execute("SELECT MAX(recorded_at) m FROM stat_observations").fetchone()[0]
    base = datetime.fromisoformat(cur) if cur else datetime.now(timezone.utc)
    return (base + timedelta(hours=1)).isoformat()


def _partial_poll(d1_store, conn, team_id=1):
    """A later, unrelated writer: ONE team, ONE metric, a newer recorded_at."""
    return d1_store.upsert_stat_observations(
        [{"subject_type": "team", "subject_id": team_id, "season": SEASON, "week": WEEK,
          "stat_key": "fcs_rating", "value": 12.0, "source": "cfbd",
          "recorded_at": _next_stamp(conn)}])


# --------------------------------------------------------------- Task 4 (selection)

def test_complete_pull_survives_a_later_partial_poll(d1):
    """A poll row must not displace the complete pull. RED on v62: only the poll survives."""
    dw, dstore, conn, _ = d1
    assert dw.snapshot_team_analytics(TEAMS, KEYS, SEASON, WEEK, authoritative_pull=True) == 9
    _partial_poll(dstore, conn)

    got = dw.load_team_analytics(SEASON)
    assert {r["name"] for r in got} == {"Alpha", "Bravo", "Delta"}, \
        "the complete pull's teams must still be selected"
    for r in got:
        for k in KEYS:
            assert r.get(k) is not None, f"{r['name']}.{k} disappeared from the served set"


def test_serving_door_requires_a_published_complete_pull(d1):
    """Unmarked rows are NOT silently promoted: serving falls back ([] -> disk), while the
    historical/backtest door still reads them explicitly."""
    dw, _dstore, _conn, _ = d1
    dw.snapshot_team_analytics(TEAMS, KEYS, SEASON, WEEK, publish=False)
    assert dw.load_team_analytics(SEASON) == [], "unmarked data must not be promoted"
    raw = dw.load_team_analytics_raw(SEASON, WEEK)
    assert {r["name"] for r in raw} == {"Alpha", "Bravo", "Delta"}, \
        "the explicit historical door must still read unmarked archived data"


def test_marker_with_mismatched_counts_is_not_promoted(d1):
    """A marker whose counts do not match the table is a partial/failed pull: fail closed."""
    dw, dstore, _conn, _ = d1
    dw.snapshot_team_analytics(TEAMS, KEYS, SEASON, WEEK, authoritative_pull=True)
    dw.publish_analytics_pull(SEASON, WEEK, stamp=T1, n_rows=9, n_teams=999,
                              identity={})
    assert dw.load_team_analytics(SEASON) == [], "a lying marker must not be served"


def test_publication_marker_carries_identity(d1):
    """Task 9 / Q6: numerics and string identity publish together, in one record."""
    dw, _dstore, _conn, _ = d1
    ident = {"Alpha": {"mascot": "Tide", "conf": "SEC", "emoji": "🏈"}}
    dw.snapshot_team_analytics(TEAMS, KEYS, SEASON, WEEK, identity=ident, authoritative_pull=True)
    pub = dw.analytics_publication(SEASON)
    assert pub and pub["week"] == WEEK and pub["n_teams"] == 3 and pub["n_rows"] == 9
    assert pub["identity"] == ident, "identity must ride the same publication record"
    assert pub["stamp"] == dw.analytics_publication(SEASON)["stamp"]


# --------------------------------------------------------------- Task 9 (atomicity)

def test_interrupted_multichunk_write_publishes_nothing(monkeypatch):
    """Chunk 2 of 2 fails -> the PREVIOUS complete pull keeps serving, never a mix."""
    conn = H.open_sqlite("append")
    _seed_teams(conn)
    stats = H.patch_d1(monkeypatch, conn)
    import d1_store
    import d1_write_path as dw

    assert dw.snapshot_team_analytics(TEAMS, KEYS, SEASON, WEEK, authoritative_pull=True) == 9
    before = dw.analytics_publication(SEASON)
    assert before is not None

    big = [{"team_id": 1000 + i, "name": f"T{i}", "sp_plus": 1.0} for i in range(500)]
    conn.executemany("INSERT INTO teams (team_id, name) VALUES (?, ?)",
                     [(t["team_id"], t["name"]) for t in big])
    conn.commit()

    real = d1_store.query_full
    seen = {"n": 0}

    def flaky(sql, params=None, timeout=60):
        if sql.startswith("INSERT INTO stat_observations"):
            seen["n"] += 1
            if seen["n"] == 2:
                raise RuntimeError("D1 HTTP 500: injected mid-pull failure")
        return real(sql, params, timeout)

    monkeypatch.setattr(d1_store, "query_full", flaky)
    # The archive is guarded by design: a failure never reaches the site (returns 0).
    # (authoritative_pull=True so the refusal is for the FAILED PULL, not the §2 gate.)
    assert dw.snapshot_team_analytics(big, ["sp_plus"], SEASON, WEEK,
                                      authoritative_pull=True) == 0
    assert dw.analytics_publication(SEASON) == before, "a failed pull must not republish"
    served = dw.load_team_analytics(SEASON)
    assert {r["name"] for r in served} == {"Alpha", "Bravo", "Delta"}, \
        "chunk 1 landed but must NOT be promoted: the marker still names the old pull"

    # RESUME: fault cleared -> the same pull completes and is published, selected once.
    monkeypatch.setattr(d1_store, "query_full", real)
    assert dw.snapshot_team_analytics(big, ["sp_plus"], SEASON, WEEK,
                                      authoritative_pull=True) == 500
    after = dw.analytics_publication(SEASON)
    assert after["n_teams"] == 500 and after["n_rows"] == 500
    resumed = dw.load_team_analytics(SEASON)
    assert len({r["name"] for r in resumed}) == 500
    assert all(r.get("sp_plus") is not None for r in resumed)


def test_publication_is_not_refused_when_d1_reports_index_inflated_writes(monkeypatch):
    """D1's meta.rows_written counts INDEX MAINTENANCE, not rows.

    Measured on the real provider (2026-09-30, scratch D1): a 2-row stat_observations
    insert reports 7. So completeness must be judged on READ-BACK ROWS. Comparing against
    that number refuses to publish forever on real D1 -- and every hermetic test would
    still be green, which is exactly why this case is pinned here.
    """
    conn = H.open_sqlite("append")
    _seed_teams(conn)
    H.patch_d1(monkeypatch, conn)
    import d1_store
    import d1_write_path as dw

    real = d1_store.query_full

    def inflated(sql, params=None, timeout=60):
        rows, meta = real(sql, params, timeout)
        if sql.startswith("INSERT INTO stat_observations") and meta:
            meta = {"rows_written": int(meta.get("rows_written", 0)) * 3 + 1}
        return rows, meta

    monkeypatch.setattr(d1_store, "query_full", inflated)
    assert dw.snapshot_team_analytics(TEAMS, KEYS, SEASON, WEEK, authoritative_pull=True) > 0
    pub = dw.analytics_publication(SEASON)
    assert pub is not None, "the pull must still publish"
    assert pub["n_rows"] == 9, f"marker must record ROWS (9), got {pub.get('n_rows')}"
    assert len(dw.load_team_analytics(SEASON)) == 3
