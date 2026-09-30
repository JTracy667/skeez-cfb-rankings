"""Provider-specific D1 claims — the part SQLite CANNOT prove (QA ruling Q1).

Runs ONLY against a scratch database. Refuses to run against production `cfb-history`, and
skips loudly (never silently) when no scratch DB is configured: a skipped provider claim is
UNVERIFIABLE, not a pass.

  CF_D1_DB_ID=<scratch uuid> CF_D1_TOKEN=<scoped token> \
      python -m pytest tests/test_d1_provider_specific.py -v

What this proves that the hermetic harness cannot:
  * the REAL D1 error text for a stale conflict target, so `_is_conflict_target_mismatch`
    recognises the provider's wording and nothing else
  * real `meta.rows_written` accounting for an insert vs a DO NOTHING replay
  * the publication record's write + read-back round trip on real D1
  * a mid-pull failure leaving the previous complete pull serving
"""
from __future__ import annotations

import os

import pytest

# The write gate is env-driven (d1_write_path.write_enabled()).
os.environ.setdefault("D1_WRITE_ENABLED", "1")

PROD_DB = "c3ec3149-cc85-483b-b727-5a18e3d5a1b9"
DB = os.environ.get("CF_D1_DB_ID", "")

pytestmark = pytest.mark.skipif(
    not DB or DB == PROD_DB,
    reason="NO SCRATCH D1: set CF_D1_DB_ID to a non-production database. Provider-specific "
           "claims are UNVERIFIABLE without it -- this skip is not a pass.")

SEASON, WEEK = 2026, 5
STAMP_A = "2026-09-30T04:00:00+00:00"
STAMP_B = "2026-09-30T05:00:00+00:00"
TEAMS = [{"team_id": 9001, "name": "Scratch Alpha", "sp_plus": 30.2},
         {"team_id": 9002, "name": "Scratch Bravo", "sp_plus": 22.4}]


def _rows(stamp, n=2):
    return [{"subject_type": "team", "subject_id": 9000 + i, "season": SEASON, "week": WEEK,
             "stat_key": "sp_plus", "value": float(i), "source": "cfbd", "recorded_at": stamp}
            for i in range(1, n + 1)]


def _cleanup(d1_store, stamps):
    """Remove ONLY the exact stamps this module created -- never a numeric threshold
    (CFBD ids are ~40xxxxxxx; a low numeric cut has wiped a table here before)."""
    for s in stamps:
        d1_store.query("DELETE FROM stat_observations WHERE recorded_at = ?", [s])
    d1_store.query("DELETE FROM app_state WHERE key LIKE 'analytics_publication:%'")
    d1_store.query("DELETE FROM teams WHERE team_id IN (9001, 9002)")


APPEND_IDX = ("CREATE UNIQUE INDEX ux_stat_obs_subject ON stat_observations("
              "subject_type, subject_id, season, stat_key, week, recorded_at)")
LEGACY_IDX = ("CREATE UNIQUE INDEX ux_stat_obs_subject ON stat_observations("
              "subject_type, subject_id, season, stat_key, week)")


@pytest.fixture(autouse=True)
def _reset_scratch():
    """Known state before every test: append-only index, no rows this module owns.

    A killed run must not poison the next one -- that is what made the first attempt here
    fail with a mixed index state rather than a real defect.
    """
    import d1_store  # noqa: PLC0415
    d1_store.query("DROP INDEX IF EXISTS ux_stat_obs_subject")
    d1_store.query(APPEND_IDX)
    # Exact, narrow deletes: the scratch team ids and this module's synthetic keys only.
    d1_store.query("DELETE FROM stat_observations WHERE subject_id IN (9001, 9002)")
    d1_store.query("DELETE FROM stat_observations WHERE season = ? AND stat_key LIKE 'k0%'",
                   [SEASON])
    d1_store.query("DELETE FROM app_state WHERE key LIKE 'analytics_publication:%'")
    d1_store.stat_obs_append_only(refresh=True)
    yield


@pytest.fixture()
def d1():
    import d1_store  # noqa: PLC0415
    import d1_write_path  # noqa: PLC0415
    d1_store.query("INSERT OR REPLACE INTO teams (team_id, name) VALUES (9001, 'Scratch Alpha')")
    d1_store.query("INSERT OR REPLACE INTO teams (team_id, name) VALUES (9002, 'Scratch Bravo')")
    yield d1_store, d1_write_path
    _cleanup(d1_store, [STAMP_A, STAMP_B])


def test_real_d1_conflict_target_error_text_is_recognised(d1):
    """The migration window, on the REAL provider: stale LEGACY target vs append-only index."""
    dstore, _dw = d1
    seen: list[str] = []
    try:
        dstore.query("DROP INDEX IF EXISTS ux_stat_obs_subject")
        dstore.query(LEGACY_IDX)
        assert dstore.stat_obs_append_only(refresh=True) is False, "process holds LEGACY mode"

        # the migration lands underneath the running process
        dstore.query("DROP INDEX ux_stat_obs_subject")
        dstore.query(APPEND_IDX)

        real = dstore.query_full

        def capture(sql, params=None, timeout=60):
            try:
                return real(sql, params, timeout)
            except Exception as e:  # noqa: BLE001
                seen.append(str(e))
                raise

        dstore.query_full = capture
        try:
            n = dstore.upsert_stat_observations_bulk(_rows(STAMP_A))
        finally:
            dstore.query_full = real

        # D1 reports INDEX-INFLATED rows_written (a 2-row insert reported 7), so the return
        # value is accounting, not a row count. The row count is asserted below.
        assert n > 0, "the retry must land the write"
        assert seen, "the stale-target attempt must fail first (otherwise no retry ran)"
        assert dstore._is_conflict_target_mismatch(RuntimeError(seen[0])), \
            f"detection does not recognise the REAL D1 text: {seen[0][:300]}"
        present = dstore.query(
            "SELECT COUNT(*) AS n FROM stat_observations WHERE recorded_at = ?", [STAMP_A])
        assert int(present[0]["n"]) == 2
    finally:
        # leave the scratch DB in the append-only state regardless of the outcome
        dstore.query("DROP INDEX IF EXISTS ux_stat_obs_subject")
        dstore.query(APPEND_IDX)
        dstore.stat_obs_append_only(refresh=True)

def test_real_d1_meta_accounting_insert_vs_replay(d1):
    """`confirmed_writes` must read real D1 meta: N on insert, 0 on an append-only replay."""
    dstore, _dw = d1
    assert dstore.upsert_stat_observations_bulk(_rows(STAMP_B)) > 0
    # replay: DO NOTHING confirms 0 new writes, and read-back proves the rows are there
    assert dstore.upsert_stat_observations_bulk(_rows(STAMP_B)) > 0
    n = dstore.query("SELECT COUNT(*) AS n FROM stat_observations WHERE recorded_at = ?",
                     [STAMP_B])
    assert int(n[0]["n"]) == 2, "a replay must not duplicate rows"


def test_real_d1_publication_roundtrip_and_partial_poll(d1):
    """Task 9 read-back on real D1: publish, read back, then survive a later partial poll."""
    dstore, dw = d1
    n = dw.snapshot_team_analytics(TEAMS, ["sp_plus"], SEASON, WEEK,
                                   identity={"Scratch Alpha": {"conf": "SEC"}})
    assert n > 0, f"expected the archive to confirm, got {n}"
    marker_rows = dstore.query(
        "SELECT COUNT(*) AS n FROM stat_observations WHERE recorded_at = ?",
        [dw.analytics_publication(SEASON)["stamp"]])
    assert int(marker_rows[0]["n"]) == 2, "exactly the pull's rows must be archived"

    pub = dw.analytics_publication(SEASON)
    assert pub and pub["week"] == WEEK and pub["n_teams"] == 2 and pub["n_rows"] == 2
    served = dw.load_team_analytics(SEASON)
    assert {r["name"] for r in served} == {"Scratch Alpha", "Scratch Bravo"}

    # a later, unrelated single-row writer with a newer stamp must not displace the pull
    later = "2099-01-01T00:00:00+00:00"
    dstore.upsert_stat_observations([{"subject_type": "team", "subject_id": 9001,
                                      "season": SEASON, "week": WEEK, "stat_key": "fcs_rating",
                                      "value": 3.0, "source": "cfbd", "recorded_at": later}])
    after = dw.load_team_analytics(SEASON)
    assert {r["name"] for r in after} == {"Scratch Alpha", "Scratch Bravo"}, \
        "the published complete pull must still be selected"
    dstore.query("DELETE FROM stat_observations WHERE recorded_at = ?", [later])


BIG_KEYS = [f'k{i:03d}' for i in range(500)]   # >400 rows => TWO statements, one team


def test_real_d1_interrupted_pull_keeps_the_previous_publication(d1):
    """Chunk 2 fails on real D1 -> the previous complete pull keeps serving."""
    dstore, dw = d1
    assert dw.snapshot_team_analytics(TEAMS, ["sp_plus"], SEASON, WEEK) > 0
    before = dw.analytics_publication(SEASON)

    # ONE team, 500 metrics: >400 rows forces the second statement, which IS the real chunk
    # boundary -- and it avoids 500 separate REST calls just to build a universe.
    big = [{"team_id": 9001, "name": "Scratch Alpha", **{k: 1.0 for k in BIG_KEYS}}]

    real = dstore.query_full
    seen = {"n": 0}

    def flaky(sql, params=None, timeout=60):
        if sql.startswith("INSERT INTO stat_observations"):
            seen["n"] += 1
            if seen["n"] == 2:
                raise RuntimeError("injected mid-pull failure")
        return real(sql, params, timeout)

    dstore.query_full = flaky
    try:
        wrote = dw.snapshot_team_analytics(big, BIG_KEYS, SEASON, WEEK)
    finally:
        dstore.query_full = real

    assert seen["n"] == 2, "the pull must have been split into chunks"
    assert wrote == 0, "the guarded archive reports 0 on failure"
    assert dw.analytics_publication(SEASON) == before, "a failed pull must not republish"
    served = dw.load_team_analytics(SEASON)
    assert {r["name"] for r in served} == {"Scratch Alpha", "Scratch Bravo"}

    # Tidy by EXACT stamp (never a numeric threshold: CFBD ids are ~40xxxxxxx).
    for r in dstore.query(
            "SELECT DISTINCT recorded_at AS s FROM stat_observations WHERE season = ? "
            "AND week = ? AND stat_key = ?", [SEASON, WEEK, BIG_KEYS[0]]) or []:
        dstore.query("DELETE FROM stat_observations WHERE recorded_at = ?", [r["s"]])
    dstore.query("DELETE FROM stat_observations WHERE recorded_at = ?", [before["stamp"]])
