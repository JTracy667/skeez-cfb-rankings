# Work Order — Performance Optimization: HANDOFF

Branch `perf/d1-snapshot-safety` · base `9e0fba8` · **not deployed** (prod is v62)
25 files, +2,731/−170. One handoff, staged in three commits as agreed.

| commit | stage |
|---|---|
| `b01da16` | docs: the work order, my QA questions, QA's answers |
| `c0f756e` | correctness: Tasks 4 + 9 + 6, Task 3 opt-out ordering |
| `1c0d98d` | serving perf: Tasks 2, 3, 5 |
| `33b58e5` | Task 6 provider-specific tests + the rows_written fix |
| `911094c` | identity serving fix |
| `02fe99e` | frontend: Tasks 1, 7, 8 + the page-script gate + doc updates |

## Task by task

| # | what changed | where | VERIFIED |
|---|---|---|---|
| 1 | Projections & Odds load on first activation of that tab, never at startup. Independent feeds — either can fail and the other still renders, with a retryable error (never the generic "No data"). Loading/error state; generation counter drops stale responses; duplicate activation cannot start a second request; a stale tab refetches on reopen. | `analytics.html` | **live**: startup trace is `/api/analytics` only, 0 projections calls; activation issues exactly 1+1 and renders a 177,988-byte pane |
| 2 | `/api/projections` cached by published identity `(season, stamp, week, live week, composite_version())`; `composite_version()` is a hash of the live weights so a config change self-invalidates; disk fallback is a distinct identity; errors are never cached | `app.py` | hermetic `tests/test_projection_cache.py` |
| 3 | One marker read per request (a sentinel passes "no record" down instead of re-reading); `teams` identity read behind a 300 s TTL invalidated by local writes; the `ANALYTICS_FROM_D1=0` opt-out now performs **zero** D1 reads | `d1_write_path.py`, `app.py` | red→green on the opt-out (3 marker reads → 0) |
| 4 | Selection is no longer `recorded_at = MAX(recorded_at)`. A later partial write can no longer become the payload: the serving door selects the stamp named by the publication marker. | `d1_write_path.py` | red→green: `{'Alpha'}` → all three teams with every metric intact |
| 5 | Serving index. **The WO's hunch was wrong and the measurement says so**: production's own `EXPLAIN QUERY PLAN` shows *both* selectors doing `SEARCH o USING INDEX ix_stat_obs_season_key_week (season=?)` — a season-wide scan. At density matched to production, the candidate index takes the new selector 111.7 ms → **19.0 ms** (old: 135.4 → 18.9). **Prepared, NOT applied.** | `d1/migrations/2026-09-30_stat_obs_serving_index.sql` | `scripts/analytics_query_plan.py` (prod plan + synthetic density) |
| 6 | Writer mode is re-detected and the whole write retried **once**, only on a positively identified conflict-target mismatch. Ordinary D1 failures are not retried. Append-only replay (0 confirmed) is proven by read-back and counted as satisfied. | `d1_store.py` | **scratch D1**: the real error text `ON CONFLICT clause does not match any PRIMARY KEY or UNIQUE constraint: SQLITE_ERROR` is recognised; the legacy→append migration window recovers live |
| 7 | Only the visible section renders. Each activation re-renders from `teams` + the live query (no blank/stale tab). Search debounced 200 ms and no longer rebuilds 15 tables per keystroke. | `analytics.html` | hermetic: 2 keystrokes inside the window → 1 render; hidden sections never rebuilt; filtered/empty search and tab switch asserted |
| 8 | With no `?week=N` the current-week and weeks requests run **concurrently**; with an override, current-week is not fetched at all. Either failing still renders a usable schedule; if weeks can't load, the resolved week is kept instead of falling back to the empty dropdown's week 1. | `schedule.html` | hermetic (overlap asserted with both in flight) + **live**: 14 weeks, week 5 selected, 59 matchup cards |
| 9 | A complete pull is published as ONE `app_state` record — stamp, counts, identity — written **last**. Numerics and identity therefore switch together, and a mid-write reader or a failed later batch cannot serve a partial pull. | `d1_write_path.py`, `scripts/publish_analytics_publication.py` | red→green hermetic; **real D1** round-trip, partial-poll survival, chunk-2 failure keeps the previous publication |

## Commands and results

```
python scripts/run_enforcement_tests.py        # the blocking gate
  -> served==D1 parity + data/ read allowlist + node --test tests/js/*.test.mjs
  -> ENFORCEMENT GATE: PASS

python -m pytest -q tests/                     # whole Python suite, scoped to tests/
  -> 129 passed, 4 skipped, 0 failed (2m20s)

node --test tests/js/*.test.mjs                # page scripts
  -> 12 passed, 0 failed

node scripts/verify_pages_live.mjs             # the same pages against the LIVE API
  -> ALL LIVE PAGE CHECKS PASSED
     analytics SP+ 687 rows · 0 projections calls at startup · pane populated on activation
     schedule 14 weeks · Week 5 · 2026 Season · 59 Games · 0 page JS exceptions

CF_D1_DB_ID=<scratch> python -m pytest tests/test_d1_provider_specific.py
  -> 4 passed   (skips LOUDLY without the scratch pin -- a skip there means UNVERIFIABLE)
```

The 4 skips are the provider-specific tests without a scratch pin. Nothing else is skipped.

## Production state after this work

- **No deploy.** The image still builds from the local tree and prod is v62.
- **One production write, approved by Jeff**: the analytics publication marker (a DATA change,
  not code). `app_state.analytics_publication:2026` → stamp `2026-09-30T16:18:36Z`, week 5,
  34,552 rows, 682 teams, 685 identity teams. Rehearsed on the scratch D1 first (blackout
  reproduced → publish → serving recovered the exact numbers).
- `/api/analytics` HTTP 200, 687 teams, Georgia `sp_plus` 30.2. `/api/health` `archive.count 0`.
- The marker is **inert under v62** and load-bearing under the new code: v62 selects by
  `MAX(recorded_at)`, the branch selects by the marker. Deploying the branch without the marker
  would have served no numerics at all (composite imputing 50 for every team).

## Unresolved tradeoffs / open items

1. **The index migration is APPLIED to production** (`2026-09-30_stat_obs_serving_index.sql`,
   applied 2026-10-01 alongside the v63 code deploy, with Jeff's authorisation). Verified by
   read-only EXPLAIN QUERY PLAN on production: both selectors moved from
   `ix_stat_obs_season_key_week (season=?)` to
   `ix_stat_obs_serving (season=? AND week=? AND subject_type=? AND recorded_at=?)`.
   Rollback remains one statement (`DROP INDEX IF EXISTS ix_stat_obs_serving;`).
2. **Regression caught in review and fixed** (`911094c`): serving identity had been switched to
   the marker's snapshot, which froze streak/conf/mascot at the publication stamp and broke the
   documented "D1 identity empty → disk" contract. Identity is served from the LIVE
   `analytics_identity` key; the snapshot is audit/rollback only.
3. **Pre-existing, NOT caused by this branch** — reproduced with two untouched modules
   (`test_analytics_archive.py` then the guard):
   `test_no_disk_reads_in_serving.py` flags `week_schedule.json <- load_schedule()` as unlisted
   whenever another module runs first (the failing scope set varies: SERVE, BUILD, or both), and
   `test_d1_results_keep_up_with_the_served_week` fails on the data condition "no scored games in
   D1 at all". Both are order-/data-dependent and were reported rather than papered over.
4. **Pre-existing landmine**: a bare `pytest` from the repo root also collects `scripts/`, where
   `scripts/test_fbs_line_scope.py` queries D1 at import and raises `SystemExit` → pytest reports
   INTERNALERROR and runs nothing. The gate now scopes to `tests/`.
5. `data/*.json` runtime mirrors changed during test runs and are deliberately **not** committed;
   `data/line_history.json` is untouched.
6. Provider-specific claims (D1 error text, `meta` accounting, retry semantics) are verified
   against a **scratch** D1 (`cfb-perf-test`); production D1 was never used as a test bed.
