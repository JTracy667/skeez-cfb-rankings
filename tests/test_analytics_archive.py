"""The analytics archive: the served dataset must also live in D1.

WHY THIS EXISTS: the dataset the site serves is data/cfbd_analytics.json — a file that is
COMMITTED to git and COPY'd into every image, then overwritten in place by a pull. So a
deploy reverted the analytics (the 41 advanced metrics included) to the build-time copy and
nothing but the running container's disk ever held it. It is archived into
stat_observations — the table the backtest harness already reads — whose unique index
(subject, season, week, key) gives backtests point-in-time weekly rows.

Hermetic: no network, no D1. The row builder is pure by design so it can be tested without
a database, and the writer is proved to be a no-op while D1 writes are off. The READ gate is
asserted on the flags themselves (F6), never on returned rows -- asserting rows would make
this file depend on a live store.
"""
import os

import pytest

import d1_store

import d1_write_path


@pytest.fixture()
def no_d1(monkeypatch):
    monkeypatch.delenv("D1_WRITE_ENABLED", raising=False)


TEAMS = [
    {"name": "Georgia", "sp_plus": 28.2, "off_havoc_total": 0.0771,
     "def_havoc_total": 0.1407, "net_field_pos": 1.0, "mascot": "", "conf": "SEC"},
    # A team with the whole advanced block missing (CFBD sub-dict absent).
    {"name": "Alabama", "sp_plus": 24.9, "off_havoc_total": None,
     "def_havoc_total": None, "net_field_pos": None},
    # Unresolvable name -> no rows at all.
    {"name": "Nowhere State", "sp_plus": 10.0},
]
NAME2ID = {"Georgia": 61, "Alabama": 333}
KEYS = ("sp_plus", "off_havoc_total", "def_havoc_total", "net_field_pos", "mascot")


def _rows(**kw):
    kw.setdefault("name2id", NAME2ID)
    return d1_write_path.team_analytics_rows(TEAMS, KEYS, 2026, 5, **kw)


def test_one_row_per_team_per_numeric_metric(no_d1):
    rows = _rows()
    by_key = {(r["subject_id"], r["stat_key"]): r for r in rows}
    assert (61, "sp_plus") in by_key and (333, "sp_plus") in by_key
    # 4 numeric keys on Georgia, 1 on Alabama; "mascot" is a string so it is never written.
    assert len(rows) == 5
    assert not [r for r in rows if r["stat_key"] == "mascot"]


def test_missing_value_is_skipped_not_written_as_zero(no_d1):
    rows = _rows()
    assert not [r for r in rows if r["subject_id"] == 333 and r["stat_key"] != "sp_plus"]
    assert all(r["value"] is not None for r in rows)


def test_unresolved_team_contributes_nothing(no_d1):
    assert not [r for r in _rows() if r["subject_id"] not in (61, 333)]


def test_rows_are_unique_on_the_dedupe_index(no_d1):
    """The upsert's conflict target is (subject_type, subject_id, season, stat_key, week).

    A duplicate inside one batch makes the multi-row INSERT ambiguous in SQLite, so the
    builder must never emit one.
    """
    keys = [(r["subject_type"], r["subject_id"], r["season"], r["stat_key"], r["week"])
            for r in _rows()]
    assert len(keys) == len(set(keys))


def test_season_and_week_are_carried_for_point_in_time_queries(no_d1):
    for r in _rows():
        assert r["season"] == 2026
        assert r["week"] == 5
        assert r["source"] == "cfbd"


def test_week_none_lands_as_zero_not_missing(no_d1):
    """week=0 is the schema's documented preseason/pre-week value; NULL would break the
    dedupe index (NULLs do not conflict in SQLite) and duplicate on every pull."""
    rows = d1_write_path.team_analytics_rows(TEAMS, ("sp_plus",), 2026, None,
                                             name2id=NAME2ID)
    assert rows and all(r["week"] == 0 for r in rows)


def test_writer_is_gated_off_without_d1_writes_enabled(no_d1):
    assert d1_write_path.snapshot_team_analytics(TEAMS, KEYS, 2026, 5) == 0


def test_reads_are_not_gated_by_the_write_flag(no_d1):
    """F6: a READ must not depend on the WRITE flag.

    The old coupling meant an unset `D1_WRITE_ENABLED` silently returned [] from every
    read, so callers fell back to the ephemeral disk with no warning -- and the only way to
    test that serving read D1 was to enable writes to PRODUCTION D1.
    """
    assert d1_write_path.read_enabled() is True
    assert d1_write_path.write_enabled() is False


def test_reads_are_noops_when_reads_are_disabled(monkeypatch):
    """The read gate still exists -- it is just its own flag now."""
    monkeypatch.setenv("D1_READ_ENABLED", "0")
    monkeypatch.setenv("D1_WRITE_ENABLED", "1")
    assert d1_write_path.load_team_analytics(2026) == []
    assert d1_write_path.archived_analytics_weeks(2026) == []


@pytest.mark.needs_d1
def test_all_numeric_metrics_are_archived_without_a_key_list():
    """The default (keys=None) must archive every numeric metric on the record.

    This is the anti-drift guarantee: a metric added to the site's record can never be
    missing from the archive, which is how the 41 advanced metrics were lost in the first
    place (a hand-maintained whitelist, then a parser that dropped them).
    """
    import app

    rec = {"name": "Georgia", "team_id": 61}
    rec.update({k: 0.5 for k in app.ADV_MATCHUP_FIELDS})
    rec.update({k: 0.25 for k in app.ADV_MATCHUP_RESUME_FIELDS})
    rec.update({"sp_plus": 28.2, "efficiency": 71.4, "talent": 92.1,
                "experience": 62.0, "composite": 83.1,
                "some_metric_invented_next_season": 1.23})
    rows = d1_write_path.team_analytics_rows([rec], None, 2026, 5)
    keys = {r["stat_key"] for r in rows}
    for k in list(app.ADV_MATCHUP_FIELDS) + list(app.ADV_MATCHUP_RESUME_FIELDS):
        assert k in keys, f"{k} would not be archived"
    for k in ("sp_plus", "efficiency", "talent", "experience", "composite",
              "some_metric_invented_next_season"):
        assert k in keys


@pytest.mark.needs_d1
def test_identity_keys_are_not_archived_as_metrics():
    rows = d1_write_path.team_analytics_rows(
        [{"name": "Georgia", "team_id": 61, "rank": 3, "sp_plus": 28.2}], None, 2026, 5)
    assert {r["stat_key"] for r in rows} == {"sp_plus"}


def test_build_paths_tolerate_env_absence(no_d1):
    """In the container D1_WRITE_ENABLED is on; locally it is off. Either way the pull
    path must not raise — app._store_team_analytics swallows everything."""
    import app

    assert app._store_team_analytics(TEAMS) == 0


# ── The bulk path: one pull is ~22k rows, and a 5-minute-idle container cannot afford
# ~1,800 HTTP round trips (D1 caps BOUND PARAMETERS at 100/query, i.e. 12 rows a trip).

def _rows_n(n, team_id=1):
    return [{"subject_type": "team", "subject_id": team_id, "season": 2026, "week": 5,
             "stat_key": f"metric_{i}", "value": i * 0.5, "source": "cfbd",
             "recorded_at": "2026-09-27T00:00:00+00:00"} for i in range(n)]


def test_bulk_batches_hundreds_of_rows_per_statement():
    sqls = d1_store.stat_observation_insert_sqls(_rows_n(1000))
    assert len(sqls) == 3  # 400 + 400 + 200, not 84 bound-parameter statements


def test_bulk_statements_stay_under_the_sql_text_cap():
    for sql in d1_store.stat_observation_insert_sqls(_rows_n(5000)):
        assert len(sql) <= d1_store.SAFE_SQL_CHARS + 2000  # + slack for the last piece
        assert sql.count("(") == sql.count(")")


def test_bulk_every_row_is_present_exactly_once():
    rows = _rows_n(777)
    sqls = d1_store.stat_observation_insert_sqls(rows)
    assert sum(s.count("'metric_") for s in sqls) == 777


def test_bulk_targets_the_dedupe_index_that_is_actually_live():
    """Phase 4 changed the unique key, so assert BOTH branches AND that the live statement
    matches the live index. Pinning one branch is how this test went stale the moment the key
    moved -- and a writer aimed at an index that does not exist fails every archive."""
    rows = _rows_n(2)

    legacy = d1_store.stat_observation_insert_sql(rows, append_only=False)
    assert "ON CONFLICT(subject_type,subject_id,season,stat_key,week)" in legacy
    assert "value=excluded.value" in legacy

    append = d1_store.stat_observation_insert_sql(rows, append_only=True)
    assert "ON CONFLICT(subject_type,subject_id,season,stat_key,week,recorded_at)" in append
    assert "DO NOTHING" in append

    live = d1_store.stat_observation_insert_sqls(rows)[0]
    if d1_store.stat_obs_append_only(refresh=True):
        assert live == append, "append-only is live but the writer emitted the LEGACY target"
    else:
        assert live == legacy, "the legacy index is live but the writer emitted the APPEND target"


def test_bulk_escapes_string_values():
    sql = d1_store.stat_observation_insert_sqls(
        [{"subject_type": "team", "subject_id": 1, "season": 2026, "week": 5,
          "stat_key": "o'brien_rate", "value": 1.0, "source": "cfbd",
          "recorded_at": "x"}])[0]
    assert "'o''brien_rate'" in sql


def test_bulk_renders_none_as_null_and_floats_exactly():
    sql = d1_store.stat_observation_insert_sqls(
        [{"subject_type": "team", "subject_id": 1, "season": 2026, "week": 5,
          "stat_key": "k", "value": None, "source": "cfbd", "recorded_at": "x"},
         {"subject_type": "team", "subject_id": 1, "season": 2026, "week": 5,
          "stat_key": "k2", "value": 0.1 + 0.2, "source": "cfbd",
          "recorded_at": "x"}])[0]
    assert ",NULL," in sql
    assert repr(0.1 + 0.2) in sql


@pytest.mark.needs_d1
def test_bulk_real_scale_statement_count():
    """The whole point: a real pull must be tens of statements, not thousands."""
    import app

    rows = []
    for tid in range(685):
        rec = {"team_id": tid + 1}
        rec.update({k: 0.5 for k in app.ADV_MATCHUP_FIELDS})
        rec.update({"sp_plus": 1.0, "efficiency": 1.0, "talent": 1.0, "experience": 1.0})
        rows += d1_write_path.team_analytics_rows([rec], None, 2026, 5)
    assert len(rows) > 20000
    sqls = d1_store.stat_observation_insert_sqls(rows)
    assert len(sqls) < 120, f"{len(sqls)} statements is too many for one pull"
