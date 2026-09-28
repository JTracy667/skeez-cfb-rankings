"""Phase 4 (F4): `stat_observations` is APPEND-ONLY, and the writer adapts to the live index.

WHY THIS EXISTS
The unique key used to stop at `week`, so ON CONFLICT DO UPDATE collapsed every weekly anchor
into one row: the archive could not be audited, because a pull left no trace. F4 is auditability,
not data loss.

The subtle part is the TRANSITION. The index swap is a D1 migration and cannot be atomic with an
image deploy, so the writer probes the live index and picks the matching ON CONFLICT target.
If it hardcoded either target, one side of the deploy would break archives. These tests pin both
branches, and then PROVE the live behaviour with real rows.

D1 rows are keyed on a unique probe stamp and deleted by that EXACT stamp -- never by a numeric
threshold. (A low numeric threshold once wiped the `games` table: CFBD game_ids are ~40xxxxxxx.)
"""
import json
import os

import pytest

os.environ.setdefault("CFB_SKIP_BOOTWARM", "1")
os.environ.setdefault("REFRESH_INTERVAL_SECONDS", "0")

PROBE_SEASON = 1900          # never a real season, so probe rows cannot collide with data
PROBE_TEAM = 9990001


def _d1():
    import d1_store  # noqa: PLC0415
    return d1_store


@pytest.fixture(scope="module", autouse=True)
def _require_d1():
    if not os.environ.get("CF_D1_TOKEN"):
        pytest.skip("CF_D1_TOKEN not set - the append-only proof needs the real D1.")


def _cleanup(stamps):
    """Delete probe rows by EXACT stamp. Never a range or a threshold."""
    for s in stamps:
        _d1().query("DELETE FROM stat_observations WHERE recorded_at = ?", [s])


# ----------------------------------------------------------------- pure: both SQL branches

def test_append_mode_uses_do_nothing_on_the_six_column_key():
    import d1_store as d  # noqa: PLC0415
    sql = d.stat_observation_insert_sql(
        [{"subject_type": "team", "subject_id": 1, "season": 2026, "week": 1,
          "stat_key": "sp_plus", "value": 1.0, "source": "cfbd",
          "recorded_at": "2026-09-28T00:00:00+00:00"}],
        append_only=True)
    assert "ON CONFLICT(subject_type,subject_id,season,stat_key,week,recorded_at)" in sql
    assert "DO NOTHING" in sql
    assert "DO UPDATE" not in sql


def test_legacy_mode_still_updates_so_the_writer_works_before_the_migration():
    import d1_store as d  # noqa: PLC0415
    sql = d.stat_observation_insert_sql(
        [{"subject_type": "team", "subject_id": 1, "season": 2026, "week": 1,
          "stat_key": "sp_plus", "value": 1.0, "source": "cfbd",
          "recorded_at": "2026-09-28T00:00:00+00:00"}],
        append_only=False)
    assert "ON CONFLICT(subject_type,subject_id,season,stat_key,week)" in sql
    assert sql.rstrip().endswith("value=excluded.value,source=excluded.source,"
                                 "recorded_at=excluded.recorded_at")
    # The legacy target must NOT include recorded_at: no such index exists before the
    # migration, and SQLite rejects an ON CONFLICT that matches no unique index.
    assert "week,recorded_at)" not in sql


def test_chunk_sizing_is_safe_in_both_modes():
    """The statement-size bound must use the LONGER clause so an over-long SQL never ships."""
    import d1_store as d  # noqa: PLC0415
    rows = [{"subject_type": "team", "subject_id": i, "season": 2026, "week": 1,
             "stat_key": "sp_plus", "value": 1.0, "source": "cfbd",
             "recorded_at": "2026-09-28T00:00:00+00:00"} for i in range(1200)]
    chunks = d.stat_observation_chunks(rows)
    assert chunks, "no chunks produced"
    for c in chunks:
        assert len(d.stat_observation_insert_sql(c, append_only=True)) <= d.SAFE_SQL_CHARS + 2000
        assert len(d.stat_observation_insert_sql(c, append_only=False)) <= d.SAFE_SQL_CHARS + 2000


# ----------------------------------------------------------------- real: the live behaviour

def test_probe_matches_the_live_index():
    import d1_store as d  # noqa: PLC0415
    sql = (d.query("SELECT sql FROM sqlite_master WHERE name='ux_stat_obs_subject'") or [{}])[0]
    live_append = "recorded_at" in (sql.get("sql") or "")
    assert d.stat_obs_append_only(refresh=True) is live_append, (
        "the probe disagrees with the actual index -- the writer would pick the wrong "
        "ON CONFLICT target and every archive would fail")


def test_one_archive_keeps_its_own_rows():
    """THE F4 ASSERTION. Two archives of the SAME (season, week, stat_key) must not collapse.

    In append-only mode they are two rows (history preserved). In legacy mode the second
    OVERWRITES the first (one row) -- which is exactly the defect. Either way the test asserts
    what the system actually does, so it is a control in both states, not a skip.
    """
    import d1_store as d  # noqa: PLC0415
    append = d.stat_obs_append_only(refresh=True)
    s1 = "PROBE-1900-A"
    s2 = "PROBE-1900-B"
    _cleanup([s1, s2])
    row = {"subject_type": "team", "subject_id": PROBE_TEAM, "season": PROBE_SEASON,
           "week": 1, "stat_key": "probe_metric", "value": 1.0, "source": "manual"}
    try:
        d.upsert_stat_observations_bulk([dict(row, recorded_at=s1, value=1.0)])
        d.upsert_stat_observations_bulk([dict(row, recorded_at=s2, value=2.0)])
        n = int(d.query("SELECT COUNT(*) AS c FROM stat_observations WHERE season=? AND "
                        "subject_id=? AND stat_key='probe_metric'",
                        [PROBE_SEASON, PROBE_TEAM])[0]["c"])
        if append:
            assert n == 2, (
                f"append-only is active but the second archive collapsed into the first "
                f"(rows={n}) -- the whole point of F4")
        else:
            assert n == 1, f"legacy mode should overwrite; got {n} rows"
    finally:
        _cleanup([s1, s2])
