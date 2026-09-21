# D1 write-budget finding — index maintenance makes writes cost 4× (2026-09-21)

**Finding (CTO, verified live).** A single 9-row `INSERT ... ON CONFLICT DO UPDATE` into
`games` is reported by the D1 API as **36 rows written** (`meta.rows_written = 36`,
`meta.changes = 9`). Probe receipt:

```
rows in batch: 9 | upsert returned: 36 | ledger delta: 36
last meta: {... 'changes': 9, 'rows_read': 9, 'rows_written': 36, 'total_attempts': 1}
```

**Root cause.** Cloudflare counts *index maintenance* as rows written
(developers.cloudflare.com/d1/platform/pricing → "writes to indexes are counted as rows
written"). `games` carries exactly three secondary indexes
(`ix_games_season_week`, `ix_games_home`, `ix_games_away`, d1/schema.sql L53-55), so each
game row costs 1 table write + 3 index writes = **4 rows of quota**.

**Consequences**

* The ledger accounting is *correct as written*: charging `meta.rows_written` mirrors what
  Cloudflare actually bills. Do **not** "fix" it by switching to `meta.changes` — that
  would under-count real consumption against the free tier's 100,000 rows/day.
* The 90K/day self-cap therefore lands far fewer *logical* rows than it appears:
  ~22.5K logical rows/day for `games`, ~45K for `stat_observations` (1 index),
  ~30K for `closing_lines` (replace = delete + insert + index).
* Observed in the live run: `2024:games` — 3,801 source games cost 15,204 rows written
  (4.0×). `2023:lines` 4,041 for 1,347 rows (3.0×). `2023:season_stats` 18,194 for ~8.4K.
* **Effect:** the 2021–2026 backfill cannot finish in one day at 90K/day. Expect roughly
  one season per day for the games+stats-heavy seasons; the supervisor now waits out the
  cap and resumes at 00:00 UTC (17:00 PT) automatically.

**Options (for the CEO / Jeff — infra spend is Tier 3, not taken here)**

1. **Accept** the multi-day drain (no cost). Supervisor handles it unattended.
2. **Workers Paid** ($5/mo) → 50M rows/month included, removes the daily ceiling.
   New spend → needs Jeff's approval.
3. **Trim indexes during the backfill** (drop `ix_games_home`/`ix_games_away`, re-add
   after) → halves the `games` burn; zero spend, small extra step.

**Not** a counter bug: the zero-write contract is unaffected — a statement that writes
nothing still reports 0 and raises `ConfirmedWriteError` (`scripts/selftest_zero_writes.py`,
18/18 PASS).
