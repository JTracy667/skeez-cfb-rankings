# D1 PHASE 2 — COMPLETION REPORT & STOP-CAUSE (2026-09-21)

CTO, working D1_CHECKLIST.md top-to-bottom. All Phase-2 boxes are checked with receipts below.

## 1. STOP-CAUSE of the 2022:teams halt — NAMED

**Cause: the backfill was launched as a Hermes TURN-TRACKED background process, so it was
reaped with the turn that started it — an external kill, not a crash and not a cap breach.**

Evidence:
- The first backfill was launched through the Hermes process registry (`terminal`
  background), NOT detached: `profiles/cto/processes.json` records
  `proc_56c94d68c66f` (cmd `... rm -f data/backfill.lock && python .../run_all.py >
  logs/backfill.log 2>&1`, pid 30104, `parent_session_id 20260821_194751_ca900b`).
- `profiles/cto/logs/agent.log` 12:48:17 — `tools.process_registry: One-shot exit
  lingering (bounded 600.0s) for 1 notify_on_complete background process(es):
  proc_56c94d68c66f`; 13:17:00 — `process_manage: No process with ID
  proc_56c94d68c66f` (gone).
- The worker log ends **mid-chunk with no terminal line** — no `DONE`, no
  `FAIL <tag>:`, no traceback. The script's own failure path always prints one of those
  before exiting, so the process did not exit through Python: it was killed externally.
- **It was NOT the enforcer.** At the halt the CFBD counter was 50 calls against the
  4,500/run cap, and the D1 day-delta was ~19K against the 90K cap. The enforcer's kill
  matcher also targets python command lines matching `backfill_d1`, while that run's
  command line was `run_all.py` — it could not have matched.
- Residual of the kill: a stale `data/backfill.lock` (pid 45920, dead) which then blocked
  the next resume attempt — the reason the job sat idle until it was relaunched.

**Fix (in place):** launch detached outside the turn lifetime —
`nohup python -u scripts/backfill_d1.py` via `scripts/run_backfill.sh` (commit 4aa5f39)
plus `scripts/backfill_supervisor.py` (relaunches the worker after crash/cap-stop and waits
out the daily cap to the next UTC midnight).

## 2. COUNTER FIX — rows_written counts D1-CONFIRMED writes only

- `d1_store.confirmed_writes(meta)` reads the D1 API's own accounting
  (`meta.rows_written`, else `meta.changes`). **No local fallback**: no accounting ⇒ 0.
- `d1_store._upsert` / `_replace_by` raise `ConfirmedWriteError` when a batch with rows
  returns 0 confirmed writes, so a batch can never be silently counted as written.
- `backfill_d1._save` writes `rows_written` = confirmed writes **this run** and
  `d1_ledger_written` = the day's confirmed total.
- Live proof from the running worker (`logs/backfill.log`):
  `2023:season_stats: 18194 rows confirmed  [cfbd_calls=17 rows_written=22235 d1_today=73435]`
  — the number is the API's, and it tracks the day ledger.
- Commits: `bafde50`, `5f29440` (plus `63879f3`, `4aa5f39`).

## 3. ZERO-CONFIRMED-WRITE CHUNK FAILS — self-test

`python scripts/selftest_zero_writes.py` → **18/18 PASS, exit 0**. It proves, with the
CFBD client and D1 store faked (no network):
- source rows present + 0 confirmed writes ⇒ exit non-zero, chunk **not** in `done`,
  recorded under `failed` ("0 confirmed writes for N source rows");
- genuinely empty source ⇒ `no_data`, exit 0 (not a failure);
- confirmed writes ⇒ chunk in `done` with the confirmed count persisted;
- a chunk that raises ⇒ exit non-zero, not in `done`, recorded under `failed`.
- `confirmed_writes()` unit checks: `None`/`{}`/no-accounting-key ⇒ 0; `rows_written` and
  `changes` trusted.

## 4. RECEIPTS — live D1 counts (scripts/d1_receipt.py, scripts/d1_season_receipt.py)

```
teams 671 | games 9893 | stat_observations 25251 | closing_lines 3609
rankings_daily 0 | odds_snapshots 0 | model_predictions 0 | raw_payloads 0
```
Per season: 2021 → 2454 games / 9124 stat_obs (934 rating obs) / 849 lines.
2022 → 3705 / 9193 (940) / 1413.  2023 → 3734 / 6802 (946) / 1347.  2024–2026 not yet.
Checkpoint: 16/30 chunks done, `failed={}`, `no_data=[]`, cfbd_calls 18 (cap 4,500/run),
d1_today 74,112 of the 90,000/day cap.

## 5. ENFORCEMENT / ALERT REPAIRS (found while working this)

1. **Sticky kill flag:** `d1_monitor_state.json` had `kill_alerted: true` forever — a
   stale flag silently suppresses the NEXT cap-breach alert. The monitor now re-arms it
   whenever a tick finds no breach. (state now `kill_alerted: false`)
2. **Cap detection could miss real burn:** the enforcer only compared a D1 row snapshot,
   which does not grow when an upsert UPDATEs existing keys. It now also reads the local
   confirmed-write ledger (`%LOCALAPPDATA%/hermes/d1_write_ledger.json`) and kills on
   whichever view breaches 90K.
3. **The monitor could not deliver alerts at all:** its Telegram send returned
   `HTTP 400 Bad Request` — it used the default Hermes bot token (Ekwbbot), which is not a
   member of the C-suite group. Token resolution now comes from the canonical helper
   `hermes-org/scripts/telegram_alert.py` (the token that works is the CTO bot's from
   `profiles/cto/.env`). Verified: `getChat -5304082623` → `ok, title "C suite bots"`, and
   the next monitor tick posted its milestone with no delivery error.
   *One milestone alert (14 chunks) was lost to the broken path — noted, not recoverable.*

## 6. WHAT'S NEXT / BLOCKERS

- **Next:** let the worker walk 2024 → 2026 (supervisor handles the 90K/day stop; it
  resumes at the next UTC midnight). Then Phase 3's FCS / Coaches-Poll / Massey items.
- **Blocker (CEO/Jeff):** Phase 5's `massey/fcs_rating` composite weights need Jeff's
  approval; Phase 2's app.py diff is compile-checked but the CEO diff review is owed
  before the next deploy.
- **Not a blocker:** desktop power is already pinned (powercfg: AC standby = Never,
  AC hibernate = Never), so no change and no Jeff decision is required for Phase 3.
