"""Hermetic D1 harness — run d1_store's SQL against an in-process SQLite database.

WHY (QA ruling Q1, 2026-09-30): Tasks 4/6/9 need TDD against real SQL semantics (unique
indexes, ON CONFLICT targets, chunked writes, marker publication, reader isolation), but
the work order forbids production writes and `d1_store` is a REST wrapper pointed at
production `cfb-history` with no local mode. So the QUERY BOUNDARY is patched — never the
selection logic under test.

WHAT THIS DOES NOT PROVE: Cloudflare-specific behaviour (REST error text, `meta`
accounting, request limits, retry/backoff). QA requires a scratch D1 for those; until one
is authorized those claims are UNVERIFIABLE, not passed.

Usage (inside a test):

    conn = open_sqlite("append")          # or "legacy"
    patch_d1(monkeypatch, conn)
"""
from __future__ import annotations

import pathlib
import re
import sqlite3

REPO = pathlib.Path(__file__).resolve().parents[1]
SCHEMA = REPO / "d1" / "schema.sql"

_LEGACY_IDX = ("CREATE UNIQUE INDEX IF NOT EXISTS ux_stat_obs_subject "
               "ON stat_observations(subject_type, subject_id, season, stat_key, week)")
_APPEND_IDX = ("CREATE UNIQUE INDEX IF NOT EXISTS ux_stat_obs_subject "
               "ON stat_observations(subject_type, subject_id, season, stat_key, week, "
               "recorded_at)")


def open_sqlite(mode: str = "append") -> sqlite3.Connection:
    """A fresh in-memory database with the repo schema in the requested index mode."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    sql = SCHEMA.read_text(encoding="utf-8")
    # schema.sql carries the LEGACY unique index (multi-line); strip it by regex and
    # rebuild it below in the requested mode.
    sql = re.sub(r"CREATE UNIQUE INDEX IF NOT EXISTS ux_stat_obs_subject\s+"
                 r"ON stat_observations\([^)]*\)\s*;", "", sql)
    conn.executescript(sql)
    conn.executescript(_APPEND_IDX if mode == "append" else _LEGACY_IDX)
    conn.commit()
    return conn


class Stats:
    """Call accounting: the tests assert bounded query counts with this."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.writes_confirmed = 0

    def count(self, needle: str) -> int:
        return sum(1 for s in self.calls if needle.lower() in s.lower())

    @property
    def n(self) -> int:
        return len(self.calls)


def patch_d1(monkeypatch, conn: sqlite3.Connection, stats: Stats | None = None) -> Stats:
    """Point d1_store at `conn`. Returns the call-accounting object."""
    import d1_store  # noqa: PLC0415

    st = stats or Stats()

    def _run(sql: str, params: list | None = None, timeout: int = 60):
        st.calls.append(sql)
        cur = conn.execute(sql, list(params or []))
        rows = [dict(r) for r in cur.fetchall()] if cur.description else []
        n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        meta = {"rows_written": n, "changes": n} if cur.description is None else {}
        conn.commit()
        return rows, meta

    monkeypatch.setattr(d1_store, "query_full", _run)
    monkeypatch.setattr(d1_store, "query", lambda sql, params=None, timeout=60: _run(sql, params)[0])
    # Budget guard is pacing, not correctness: keep the ledger untouched.
    monkeypatch.setattr(d1_store, "assert_headroom", lambda *a, **k: None)
    monkeypatch.setattr(d1_store, "commit_writes",
                        lambda n, meter=True: setattr(st, "writes_confirmed",
                                                      st.writes_confirmed + int(n)))
    # Index-mode probe must read the PATCHED database, not the process-wide cache.
    monkeypatch.setattr(d1_store, "_STAT_OBS_APPEND_ONLY", None)
    return st