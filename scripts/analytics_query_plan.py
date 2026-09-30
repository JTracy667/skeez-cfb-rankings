#!/usr/bin/env python3
"""Task 5 — measure the analytics serving query BEFORE choosing an index.

The work order's candidate index `(season, week, subject_type, recorded_at)` was proposed
against the OLD query, whose cost is dominated by a correlated `MAX(recorded_at)` subquery.
Task 4/9 replaced that selector with `recorded_at = ?`, so the plan must be re-measured; this
script may legitimately conclude "no new index" (QA ruling Q4: do not install it by default).

It builds a REPRESENTATIVE archive in-process (the real per-stamp shape measured from
production: rows/stamp, teams, keys, pulls/season), prints EXPLAIN QUERY PLAN and median
timings for the old and new selectors, then repeats with the candidate index present.

  python scripts/analytics_query_plan.py                 # local, representative volume
  python scripts/analytics_query_plan.py --prod          # read-only plan against production D1
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# Measured from production 2026-09-30 (scripts/publish_analytics_publication.py dry run):
ROWS_PER_STAMP, TEAMS, KEYS, PULLS = 34552, 682, 115, 13
SEASON, WEEK = 2026, 5

NEW_SQL = ("SELECT o.subject_id AS tid, o.stat_key AS k, o.value AS v, t.name AS name "
           "FROM stat_observations o LEFT JOIN teams t ON t.team_id = o.subject_id "
           "WHERE o.season = ? AND o.week = ? AND o.subject_type = 'team' "
           "AND o.recorded_at = ?")
NEW_PARAMS = [SEASON, WEEK, "STAMP-13"]

OLD_SQL = ("SELECT o.subject_id AS tid, o.stat_key AS k, o.value AS v, t.name AS name "
           "FROM stat_observations o LEFT JOIN teams t ON t.team_id = o.subject_id "
           "WHERE o.season = ? AND o.week = ? AND o.subject_type = 'team' "
           "AND o.recorded_at = (SELECT MAX(recorded_at) FROM stat_observations "
           "                     WHERE season = ? AND week = ? AND subject_type = 'team')")
OLD_PARAMS = [SEASON, WEEK, SEASON, WEEK]

CANDIDATE = ("CREATE INDEX ix_stat_obs_serving ON stat_observations("
             "season, week, subject_type, recorded_at)")


def build(conn: sqlite3.Connection) -> None:
    conn.executescript((REPO / "d1" / "schema.sql").read_text(encoding="utf-8"))
    # Production's index is the APPEND-ONLY one (v62): several pulls share (season, week).
    conn.executescript(
        "DROP INDEX IF EXISTS ux_stat_obs_subject;"
        "CREATE UNIQUE INDEX ux_stat_obs_subject ON stat_observations("
        "subject_type, subject_id, season, stat_key, week, recorded_at);")
    teams = [(i, f"Team{i:04d}") for i in range(1, TEAMS + 1)]
    conn.executemany("INSERT INTO teams (team_id, name) VALUES (?, ?)", teams)
    rows = []
    # Density matched to production: 34,552 rows per stamp for 682 teams over 115 distinct
    # keys => ~50 keys per team (not every team carries every metric).
    per_team = max(1, ROWS_PER_STAMP // TEAMS)
    for p in range(1, PULLS + 1):
        stamp = f"STAMP-{p}"
        for t in range(1, TEAMS + 1):
            off = (t * 7) % max(1, KEYS - per_team + 1)
            for k in range(off, off + per_team):
                rows.append(("team", t, SEASON, WEEK, f"key{k:03d}", float(k), "cfbd", stamp))
        if len(rows) >= 60000:
            conn.executemany("INSERT INTO stat_observations (subject_type, subject_id, season,"
                             " week, stat_key, value, source, recorded_at) VALUES (?,?,?,?,?,"
                             "?,?,?)", rows)
            rows = []
    if rows:
        conn.executemany("INSERT INTO stat_observations (subject_type, subject_id, season, week,"
                         " stat_key, value, source, recorded_at) VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.commit()


def plan(conn, sql, params):
    return [dict(r) for r in conn.execute("EXPLAIN QUERY PLAN " + sql, params)]


def timeit(conn, sql, params, n=5):
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        rows = conn.execute(sql, params).fetchall()
        ts.append((time.perf_counter() - t0) * 1000)
    return statistics.median(ts), len(rows)


def report(conn, label):
    total = conn.execute("SELECT COUNT(*) FROM stat_observations").fetchone()[0]
    print(f"\n=== {label} (archive rows: {total:,}) ===")
    for name, sql, params in (("NEW  recorded_at = ?", NEW_SQL, NEW_PARAMS),
                              ("OLD  MAX(recorded_at)", OLD_SQL, OLD_PARAMS)):
        for row in plan(conn, sql, params):
            print(f"  {name}: {row.get('detail')}")
        ms, n = timeit(conn, sql, params)
        print(f"  {name}: median {ms:.1f} ms, {n} rows returned")


def prod_plan() -> int:
    import d1_store  # noqa: PLC0415
    for name, sql, params in (("NEW  recorded_at = ?", NEW_SQL, NEW_PARAMS),
                              ("OLD  MAX(recorded_at)", OLD_SQL, OLD_PARAMS)):
        rows = d1_store.query("EXPLAIN QUERY PLAN " + sql, params)
        for r in rows:
            print(f"  {name}: {r.get('detail')}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prod", action="store_true", help="read-only plan on production D1")
    a = ap.parse_args()
    if a.prod:
        print("production D1 (read-only EXPLAIN QUERY PLAN)")
        return prod_plan()

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    t0 = time.perf_counter()
    build(conn)
    print(f"built representative archive in {time.perf_counter() - t0:.1f}s")

    report(conn, "shipped indexes (ux_stat_obs_subject, ix_stat_obs_season_key_week)")
    conn.execute(CANDIDATE)
    conn.commit()
    report(conn, "with the work order's candidate index")
    print("\nVERDICT: choose based on the numbers above; a smaller plan step is not a win if the "
          "median time is unchanged, and every index adds write amplification to a "
          "~35k-row-per-pull append-only table.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())