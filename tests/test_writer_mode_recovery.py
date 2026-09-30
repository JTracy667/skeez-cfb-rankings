"""Task 6 — a long-lived writer must survive the append-only index migration (QA Q5).

The index swap is a D1 migration and cannot be atomic with an image deploy, so a process can
hold a mode that no longer matches the live index. Recovery = refresh the detected mode and
retry the WHOLE write ONCE, and ONLY for a positively identified conflict-target mismatch.

Run: python -m pytest tests/test_writer_mode_recovery.py -v
"""
from __future__ import annotations

import os

import pytest

import d1_harness as H

os.environ.setdefault("D1_WRITE_ENABLED", "1")

STAMP = "2026-09-30T04:00:00+00:00"
ROWS = [{"subject_type": "team", "subject_id": i, "season": 2026, "week": 5,
         "stat_key": "sp_plus", "value": float(i), "source": "cfbd", "recorded_at": STAMP}
        for i in (1, 2)]


def _to_append(conn):
    """Simulate the v62 migration landing in D1 while the process is still running."""
    conn.executescript(
        "DROP INDEX ux_stat_obs_subject;"
        "CREATE UNIQUE INDEX ux_stat_obs_subject ON stat_observations("
        "subject_type, subject_id, season, stat_key, week, recorded_at);")
    conn.commit()


def test_stale_legacy_mode_refreshes_and_retries_exactly_once(monkeypatch):
    """RED on v62: the writer keeps its stale LEGACY target and every archive fails."""
    conn = H.open_sqlite("legacy")
    H.patch_d1(monkeypatch, conn)
    import d1_store

    assert d1_store.stat_obs_append_only() is False, "process has cached LEGACY mode"
    _to_append(conn)                      # the migration lands underneath us

    real = d1_store.query_full
    attempts = {"insert": 0}

    def counting(sql, params=None, timeout=60):
        if sql.startswith("INSERT INTO stat_observations"):
            attempts["insert"] += 1
        return real(sql, params, timeout)

    monkeypatch.setattr(d1_store, "query_full", counting)
    assert d1_store.upsert_stat_observations_bulk([dict(r) for r in ROWS]) == 2
    assert attempts["insert"] == 2, "one attempt + exactly one retry"
    assert d1_store.stat_obs_append_only() is True, "the mode was refreshed"
    assert conn.execute("SELECT COUNT(*) FROM stat_observations").fetchone()[0] == 2


def test_ordinary_write_failure_is_not_retried(monkeypatch):
    """Only a conflict-target mismatch may trigger the retry — never a generic D1 error."""
    conn = H.open_sqlite("append")
    H.patch_d1(monkeypatch, conn)
    import d1_store

    real = d1_store.query_full
    attempts = {"insert": 0}

    def boom(sql, params=None, timeout=60):
        if sql.startswith("INSERT INTO stat_observations"):
            attempts["insert"] += 1
            raise RuntimeError("D1 HTTP 503: service unavailable")
        return real(sql, params, timeout)

    monkeypatch.setattr(d1_store, "query_full", boom)
    with pytest.raises(RuntimeError):
        d1_store.upsert_stat_observations_bulk([dict(r) for r in ROWS])
    assert attempts["insert"] == 1, "an ordinary failure must not be retried"


def test_replay_of_a_landed_pull_is_idempotent_not_an_error(monkeypatch):
    """Append-only DO NOTHING confirms 0 new writes on replay; that is success, not failure."""
    conn = H.open_sqlite("append")
    H.patch_d1(monkeypatch, conn)
    import d1_store

    assert d1_store.upsert_stat_observations_bulk([dict(r) for r in ROWS]) == 2
    assert d1_store.upsert_stat_observations_bulk([dict(r) for r in ROWS]) == 2
    assert conn.execute("SELECT COUNT(*) FROM stat_observations").fetchone()[0] == 2


def test_zero_confirmed_with_rows_absent_still_fails_closed(monkeypatch):
    """CONTROL: the replay exemption must not become a blanket "0 writes is fine"."""
    conn = H.open_sqlite("append")
    H.patch_d1(monkeypatch, conn)
    import d1_store

    real = d1_store.query_full
    monkeypatch.setattr(
        d1_store, "query_full",
        lambda sql, params=None, timeout=60:
        ([], {"rows_written": 0}) if sql.startswith("INSERT INTO stat_observations")
        else real(sql, params, timeout))
    with pytest.raises(d1_store.ConfirmedWriteError):
        d1_store.upsert_stat_observations_bulk([dict(r) for r in ROWS])
    assert conn.execute("SELECT COUNT(*) FROM stat_observations").fetchone()[0] == 0