# D1 IMPLEMENTATION — CHECKLIST & GOAL (single source of truth)
**Goal:** skeezcfb-rankings.com backed by D1 persistent history (2021–2026, team-level + FCS),
with the live site reading from it, backfill complete, and backtest tooling running.
**Definition of DONE:** every box checked with a receipt; Jeff informed only at ⏸/✅/❌ events.

CTO: work top-to-bottom. Check a box ONLY with a receipt (commit hash / D1 row count / command output).
If blocked >15 min on any item: post the blocker to the CSuite group and STOP — do not skip ahead.

## PHASE 1 — FOUNDATION ✅ (complete, verified)
- [x] D1 database cfb-history created (live: uuid c3ec3149…)
- [x] Schema: 9 tables + indexes (d1/schema.sql, commit 5f6d2b2)
- [x] Store module verified live (d1_store.py, commit 7f165f7)
- [x] API capability catalog (docs/API_CATALOG.md + api_catalog.json, commit 3c32a00)
- [x] Watchdog breaker (hermes-org cc26829)
- [x] API budget audit (CFBD 30K/mo, Odds 20K/mo, PropLine 5K/day)

## PHASE 2 — REFACTOR ✅ (complete 2026-09-21; receipts inline)
- [x] backfill_d1.py imports app's shared cfbd_shared.py (private client+map deleted) — commit bdd19b7; VERIFY `grep -n "urllib\|cfbd_key\|CFBD_BASE\|api.cfbd" scripts/backfill_d1.py` → no match (committed 4aa5f39)
- [x] rows_written counter counts ACTUAL D1-confirmed writes (asserts on API response) — d1_store.confirmed_writes() + ConfirmedWriteError, no local fallback (commits bafde50/5f29440); live log `2023:season_stats: 18194 rows confirmed [..] d1_today=73435`
- [x] chunk with 0 confirmed writes FAILS, never marks done — self-test proves it: `python scripts/selftest_zero_writes.py` → 18/18 PASS, exit 0
- [x] stop-cause of 2022:teams halt named in report — D1_PHASE2_REPORT.md §1: the run was killed with its Hermes turn (registry proc_56c94d68c66f reaped at 13:17; log ends mid-chunk with no DONE/FAIL/traceback) — not a crash, not a cap breach (50/4,500 calls, ~19K/90K rows)
- [x] app.py edit smoke-checked (compiles; diff reviewed by CEO before next deploy) — `python -m py_compile app.py` OK (commit bdd19b7); CEO diff review still owed pre-deploy

## PHASE 3 — BACKFILL ✅ (COMPLETE 2026-09-21; all 30 chunks, supervisor rc=0)
- [x] Desktop power pinned — receipt `powercfg /q`: AC standby = Never (0x0), AC hibernate = Never (0x0). Already pinned; no change needed (nothing to ask Jeff)
- [x] Backfill running detached (survives turn ends), lock file present — worker pid 34468 (started 13:29:48 PT via scripts/run_backfill.sh + nohup) is ALIVE while its launching shell (pid 31300) is DEAD; supervisor pid 37432 heartbeating in logs/supervisor.log; lock data/backfill.lock = 34468
- [x] 2021: all 5 endpoints verified in D1 (receipt: D1 row counts) — teams 671 · games 2454 · stat_observations 9124 (incl. 934 rating obs) · closing_lines 849
- [x] 2022 complete — games 3705 · stat_observations 9193 (940 rating obs) · closing_lines 1413; all 5 chunks in checkpoint `done`
- [x] 2023, 2024, 2025 complete — ✅ 2026-09-21: supervisor finished ALL 30 chunks (rc=0). games by season: 2021 2,454 · 2022 3,705 · 2023 3,734 · 2024 3,801 · 2025 3,831 · 2026 3,679 (total 21,204); stat_observations by season 9,449 / 9,518 / 9,648 / 9,657 / 9,827 / 9,495 (total 57,594); closing_lines 849 / 1,413 / 1,347 / 1,507 / 1,547 / 521 (total 7,184). Receipt: `logs/supervisor.log` "COMPLETE: all 30 chunks done/no_data"
- [x] 2026 wks 1–3 complete — receipt: 2026 games 3,679 · lines 1,042 · ratings 2,800 · season_stats 15,906 confirmed writes
- [x] Name→ID gaps closed — the run ended with `UNMATCHED ['Albany','Southeastern Louisiana','UTRGV']` (CFBD ratings emit an alias, not /teams' school: UAlbany / SE Louisiana / UT Rio Grande Valley). Fixed by a SEPARATE alias namespace (`cfbd_shared.team_aliases()`, canonical names still win) + `scripts/backfill_unmatched_names.py` re-ingest: 14 confirmed writes (only 2025–2026 had such rows).
- [x] FCS games stored + division tag (receipt 2026-09-21): `/games` with no filter already returns every classification (2025: 3,831 games / 563 non-FBS schools); division tag delivered as `teams.classification` (fbs 138 · fcs 128 · ii 170 · iii 248; 4,046 confirmed writes, `scripts/backfill_fcs_extras.py`)
- [ ] FCS-specific STATS — ⛔ BLOCKED, not deliverable from CFBD: `/stats/season?division=fcs` and `/games?division=fcs` SILENTLY ignore the filter (return the same FBS rows, probed live). Needs another source; do not fake it.
- [x] CFBD FCS Coaches Poll 2021–2026 stored as fcs_rating observations — receipt: 5,514 confirmed writes (2021:1003 · 2022:1030 · 2023:1006 · 2024:1083 · 2025:1083 · 2026:309), 96 CFBD calls, `scripts/backfill_fcs_extras.py`
- [ ] Massey ratings 2021–2026 — ⛔ BLOCKED: CFBD `/ratings/massey` returns 0 rows (endpoint unpopulated); needs an external archive scrape (masseyratings.com), a separate build
- [x] Daily caps respected every day (receipt: cfbd_calls + rows_written logged per run) — 2026-09-21 ledger closed at **288,330** D1-confirmed rows written against the **2,000,000/day** guard on Workers PAID (50M/month pool ⇒ ~0.6% of the month). Never near the guard; no free-tier daily ceiling exists.

## PHASE 3.5 — STANDING BUDGET METERS (CEO directive: counters always rolling, never ad-hoc)
- [ ] Daily usage ledger per API, appended on every call batch: CFBD (calls/day), Odds API (calls/month), PropLine (calls/day), D1 (confirmed rows written/day) — persisted to data/budget_ledger.json
- [ ] Caps recorded from Jeff's stated plan (authoritative): CFBD **30,000 calls/month** (cheap to tier up if needed) · Odds API 20,000/month · PropLine **5,000/day** · D1 Workers Paid 50M rows-written/month. Ledger tracks against THESE, alerts at 80%/95%.
- [ ] Threshold alerts wired to the monitor: any API at 80% of its real cap → group alert; 95% → auto-pause that consumer (same enforcement pattern as D1)
- [ ] "What's our burn?" answerable from the ledger file in one read — no polling, no estimates


- [ ] Hourly refresh appends odds_snapshots (receipt: row delta after one poll)
- [ ] Nightly rankings_daily archive job scheduled (receipt: first run)
- [ ] model_predictions written pre-kickoff
- [ ] Degraded mode: budget breach → stop calling + staleness flag + group alert (test proven)

## PHASE 5 — BACKTESTING ⏳
- [ ] D1→local export path works
- [ ] Baseline backtest report on 2021–2025 (per-stat predictive scores) delivered to group
- [ ] massey/fcs_rating added to composite config ( Jeff approves weights)

## MONITORING (CEO side — Jeff never babysits)
- Automated status check every 30 min: backfill process alive? checkpoint advancing?
- Posts to CSuite group ONLY on: milestone ✅ / stall >30 min ❌ / budget breach ⚠️. Silent = healthy.
- Any ❌ twice in a row → CEO informed with diagnosis, not just the alert.
