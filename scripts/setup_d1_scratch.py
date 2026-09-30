"""Apply d1/schema.sql to a scratch D1 database, then mirror production's append-only index.

Run with CF_D1_DB_ID + CF_D1_TOKEN pointing at the SCRATCH database.
"""
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

db = os.environ.get("CF_D1_DB_ID", "")
if not db or db.startswith("c3ec3149"):
    print("REFUSING: CF_D1_DB_ID is unset or points at PRODUCTION cfb-history", file=sys.stderr)
    sys.exit(2)

import d1_store  # noqa: E402

print(f"target database: {db}")

sql = (REPO / "d1" / "schema.sql").read_text(encoding="utf-8")
# Strip comment-only lines, then split on statement boundaries.
lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
statements = [s.strip() for s in re.split(r";\s*(?=\n|$)", "\n".join(lines)) if s.strip()]

ok = 0
for st in statements:
    head = " ".join(st.split())[:60]
    try:
        d1_store.query(st)
        ok += 1
    except Exception as e:  # noqa: BLE001
        print(f"  FAILED: {head}... -> {e}")
print(f"schema: {ok}/{len(statements)} statements applied")

# Production state (v62): the unique index includes recorded_at.
print("mirroring the append-only index...")
d1_store.query("DROP INDEX IF EXISTS ux_stat_obs_subject")
d1_store.query("CREATE UNIQUE INDEX ux_stat_obs_subject ON stat_observations("
               "subject_type, subject_id, season, stat_key, week, recorded_at)")

idx = d1_store.query("SELECT name, sql FROM sqlite_master WHERE type='index' "
                     "AND tbl_name='stat_observations'")
for r in idx:
    print("  ", r.get("name"), "|", (r.get("sql") or "")[:90])
tables = d1_store.query("SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table'")
print("tables:", tables[0]["n"] if tables else "?")
print("probe (append-only detection):", d1_store.stat_obs_append_only(refresh=True))