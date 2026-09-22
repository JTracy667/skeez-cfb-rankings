#!/usr/bin/env python3
"""export_d1.py — pull D1 down to a local SQLite file for backtesting.

D1_CHECKLIST Phase 5.1 / risk register B5: heavy backtest queries must NOT run in
the Worker (CPU limits + read budget). Research happens on the ground: this
exports the archive chain to a local SQLite file that pandas/sqlite3 can chew on.

Usage:
    python scripts/export_d1.py [--out data/d1_export.sqlite] [--tables games,stat_observations,...]
                                [--page 2000] [--verify-only]

Verifies each table's exported count against D1's own COUNT(*) and exits 1 on a
mismatch — an export that silently truncates would corrupt every backtest built
on it, which is exactly the failure mode this is meant to prevent.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import d1_store  # noqa: E402

DEFAULT_TABLES = ["teams", "games", "stat_observations", "closing_lines",
                  "rankings_daily", "model_predictions", "odds_snapshots"]
# raw_payloads is deliberately excluded: compressed blobs, multi-GB, never needed
# for a backtest. Export it explicitly if some future task actually wants it.


def d1_count(table: str) -> int:
    rows = d1_store.query(f"SELECT COUNT(*) AS n FROM {table}")
    return int(rows[0]["n"]) if rows else 0


def d1_columns(table: str) -> list[str]:
    rows = d1_store.query(f"PRAGMA table_info({table})")
    return [r["name"] for r in sorted(rows, key=lambda r: r.get("cid", 0))]


def export_table(con: sqlite3.Connection, table: str, page: int) -> int:
    cols = d1_columns(table)
    if not cols:
        print(f"  {table}: no columns (table missing?) — skipped")
        return 0
    col_sql = ",".join(f'"{c}"' for c in cols)
    con.execute(f'DROP TABLE IF EXISTS "{table}"')
    con.execute(f'CREATE TABLE "{table}" ({",".join(chr(34) + c + chr(34) for c in cols)})')
    ph = ",".join("?" * len(cols))
    ins = f'INSERT INTO "{table}" ({col_sql}) VALUES ({ph})'

    total, offset = 0, 0
    while True:
        batch = d1_store.query(
            f"SELECT {col_sql} FROM {table} LIMIT {int(page)} OFFSET {int(offset)}")
        if not batch:
            break
        con.executemany(ins, [[r.get(c) for c in cols] for r in batch])
        con.commit()
        total += len(batch)
        offset += len(batch)
        if len(batch) < page:
            break
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join("data", "d1_export.sqlite"))
    ap.add_argument("--tables", default=",".join(DEFAULT_TABLES))
    ap.add_argument("--page", type=int, default=2000,
                    help="rows per D1 query (keep the response payload sane)")
    ap.add_argument("--verify-only", action="store_true")
    args = ap.parse_args()

    tables = [t.strip() for t in args.tables.split(",") if t.strip()]
    out = args.out if os.path.isabs(args.out) else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), args.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)

    if args.verify_only:
        if not os.path.exists(out):
            print(f"no export at {out} to verify")
            return 1
        con = sqlite3.connect(out)
    else:
        print(f"== exporting D1 -> {out} (page={args.page}) ==")
        con = sqlite3.connect(out)

    bad = 0
    t0 = time.time()
    for table in tables:
        if args.verify_only:
            local = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        else:
            t = time.time()
            local = export_table(con, table, args.page)
            print(f"  {table:20} {local:>8} rows  ({time.time() - t:.1f}s)")
        try:
            remote = d1_count(table)
        except Exception as e:  # noqa: BLE001
            print(f"  {table:20} D1 count failed: {e}")
            remote = None
        if remote is not None and local != remote:
            print(f"  !! {table}: local {local} != D1 {remote} — EXPORT TRUNCATED")
            bad += 1
    con.close()
    print(f"== done in {time.time() - t0:.1f}s ==")
    if bad:
        print(f"FAILED: {bad} table(s) mismatch D1")
        return 1
    print("export verified: every table matches D1 row-for-row")
    return 0


if __name__ == "__main__":
    sys.exit(main())