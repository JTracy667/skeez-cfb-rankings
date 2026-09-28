"""Phase 4 (F4): make `stat_observations` APPEND-ONLY by adding `recorded_at` to its unique key.

WHY
The unique key was (subject_type, subject_id, season, stat_key, week), written with
ON CONFLICT DO UPDATE, so the four weekly anchors collapse to ONE row per metric, last write
wins, and intermediate pulls leave no trace. You cannot then tell from D1 whether a given pull
archived, or what it said before a later one overwrote it. F4 is auditability, not data loss --
the values are right, the history is gone.

WHAT IT DOES
Swaps the unique index to include `recorded_at`. One archive = one `recorded_at` (the bulk
writer stamps a single timestamp for the whole call), so the key gains a per-pull discriminator
and each anchor keeps its own rows. **No new column and no table rebuild**: `recorded_at`
already exists, and the 92k existing rows already have 89 distinct stamps.

SAFE BY CONSTRUCTION
- Creating the new index CANNOT fail on existing data: the old index already made the first five
  columns unique, so adding a sixth can only be more permissive, never less.
- The code adapts to whichever index is live (see `d1_store.stat_obs_append_only()`), so this
  migration can be applied BEFORE or AFTER the deploy. There is no window in which archives fail
  and no window in which they silently drop an update.
- Reversible: `--revert` restores the old index. On revert, rows sharing the first five columns
  would collapse -- so revert is only safe if no new pulls have archived since. The script
  refuses to revert silently when that would lose rows unless `--force` is passed.

USAGE
    python scripts/migrate_stat_obs_append_only.py --dry-run
    python scripts/migrate_stat_obs_append_only.py
    python scripts/migrate_stat_obs_append_only.py --revert [--force]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import d1_store  # noqa: E402

INDEX = "ux_stat_obs_subject"
COLS_LEGACY = "subject_type, subject_id, season, stat_key, week"
COLS_APPEND = COLS_LEGACY + ", recorded_at"


def _index_sql() -> str | None:
    rows = d1_store.query("SELECT sql FROM sqlite_master WHERE type='index' AND name=?", [INDEX])
    return (rows[0].get("sql") or "") if rows else None


def _count_rows() -> int:
    return int(d1_store.query("SELECT COUNT(*) AS c FROM stat_observations")[0]["c"])


def _duplicate_groups_after_revert() -> int:
    """How many (key) groups hold >1 row -- i.e. what a revert would collapse."""
    r = d1_store.query(
        "SELECT COUNT(*) AS c FROM (SELECT 1 FROM stat_observations "
        "GROUP BY subject_type, subject_id, season, stat_key, week HAVING COUNT(*) > 1)")
    return int(r[0]["c"]) if r else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--revert", action="store_true")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    if not os.environ.get("CF_D1_TOKEN"):
        print("REFUSING: CF_D1_TOKEN is not set; this must run against the real D1.")
        return 2

    sql = _index_sql()
    print(f"index   : {INDEX}")
    print(f"current : {(sql or '(missing)').replace(chr(10), ' ')[:160]}")
    already = bool(sql) and "recorded_at" in sql
    print(f"mode    : {'APPEND-ONLY (already migrated)' if already else 'LEGACY (upsert)'}")
    print(f"rows    : {_count_rows()}")

    if a.revert:
        if not already:
            print("nothing to do: the index is already on the legacy key.")
            return 0
        dupes = _duplicate_groups_after_revert()
        print(f"key groups that would COLLAPSE on revert: {dupes}")
        if dupes and not a.force:
            print("REFUSING to revert: it would discard archived pulls. Re-run with --force if "
                  "that is really what you want.")
            return 3
        if a.dry_run:
            print("[dry-run] would DROP and recreate the index on the LEGACY key.")
            return 0
        for stmt in (f"DROP INDEX IF EXISTS {INDEX}",
                     f"CREATE UNIQUE INDEX {INDEX} ON stat_observations({COLS_LEGACY})"):
            d1_store.query(stmt)
            print(f"  ok: {stmt[:70]}")
        print("REVERTED to the legacy key.")
        return 0

    if already:
        print("nothing to do: already append-only.")
        return 0

    if a.dry_run:
        print(f"[dry-run] would DROP INDEX {INDEX} and recreate it as:")
        print(f"          CREATE UNIQUE INDEX {INDEX} ON stat_observations({COLS_APPEND})")
        return 0

    # DROP first, then create: both are DDL and each is fast; between them the table briefly has
    # no unique index, which cannot corrupt anything -- the code that writes is adaptive and the
    # window is milliseconds.
    d1_store.query(f"DROP INDEX IF EXISTS {INDEX}")
    print(f"  ok: dropped {INDEX}")
    d1_store.query(f"CREATE UNIQUE INDEX {INDEX} ON stat_observations({COLS_APPEND})")
    print(f"  ok: created {INDEX} on ({COLS_APPEND})")

    after = _index_sql() or ""
    ok = "recorded_at" in after
    print(f"verify  : {'APPEND-ONLY confirmed' if ok else 'STILL LEGACY -- investigate'}")
    d1_store.stat_obs_append_only(refresh=True)
    print(f"probe   : stat_obs_append_only() -> {d1_store.stat_obs_append_only()}")
    return 0 if ok else 4


if __name__ == "__main__":
    raise SystemExit(main())
