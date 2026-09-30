# Work Order: CFB Site Performance and D1 Serving Safety

**To:** CTO  
**From:** Jeff / QA review  
**Repository:** `C:\Users\jtracy\dev\cfb-power-rankings`  
**Review baseline:** `main` at `9e0fba8d7f0f29009a33c6fa1497991f65566484` (v62 reported by the live health endpoint during QA review)  
**Priority:** Correct D1 snapshot safety first; then improve initial-page work and repeated API latency while preserving current live behavior and model semantics.  
**Scope:** Implement, test, and report. Do not deploy or change production without Jeff's separate authorization.

## Goal

Reduce unnecessary initial-page work and repeated projections/D1 serving latency, while correcting append-only analytics publication so partial later writes cannot replace a complete served pull.

## Current verified baseline

- `python -m pytest -q`: **112 passed in 157.17 seconds** on the review machine. This is not proof that D1-backed parity tests actually ran: `tests/test_served_equals_d1.py:38-42` skips its module when `CF_D1_TOKEN` is absent. Report passed/skipped/xfail counts separately, and run the parity gate with authorized read credentials before claiming D1 correctness.
- Live endpoints `/api/rankings`, `/api/analytics`, and `/api/projections` returned HTTP 200 during QA probes.
- `/api/projections` returned 687 rows and took about **3.3–4.3 seconds** across four sequential requests.
- `/api/analytics` returned 687 teams and took about **0.48–0.69 seconds** across four sequential requests.
- `/api/rankings` returned 25 teams and took about **0.29–1.07 seconds** across four sequential requests.
- Times above are a small-sample baseline, not a formal load test. Record conditions and repeat the same probes after changes.
- Existing local user data changes must be preserved. At work-order creation, `data/line_history.json` was already modified; do not overwrite, revert, or stage unrelated data files.

## Guardrails

1. Do not change any model constants, formulas, ranking order, projection semantics, data-source semantics, or displayed values as part of performance work.
2. Preserve D1-first serving and its disk fallback behavior. Do not trade correctness or freshness for a faster response.
3. Do not weaken or delete tests to make the suite pass. Add tests before implementation and demonstrate the expected failure first (TDD).
4. Do not add credentials, tokens, private data, or secrets to source, this work order, logs, or chat.
5. No production deploy, D1 migration, production write, or live configuration change under this work order. A schema change may be prepared and tested locally; submit migration steps and rollback plan for approval before applying it to production.
6. Keep each task focused. Report files changed, exact test commands/results, benchmark receipts, and any unresolved tradeoff. No “faster” claim without before/after measurements.

## Task 1 — Defer hidden-tab projections and odds on Analytics (P2)

**Evidence:** `analytics.html:557-560` awaits `fetchAnalytics()` and then calls `fetchProjectionsAndOdds()`, including `/api/projections`, whose live response took roughly 3.3–4.3 seconds. Important correction: `fetchAnalytics()` already reveals and renders the main content at `analytics.html:227-229`, so projections do **not** block first content. The opportunity is to defer an unnecessary request and expensive hidden-tab rendering, not to fix an alleged blank-screen wait.

**Implementation direction:**
- Keep the existing immediate primary analytics rendering. The page **does** have a `Projections & Odds` tab (`analytics.html:182`): load its data when that tab is first opened, rather than on startup.
- Keep projections and odds independent so an error or slow response from one does not prevent the other from rendering. Preserve the existing 5-minute and visibility-change refresh behavior for an active Projections & Odds tab; when hidden, fetch fresh data on reopening if it is due. Avoid duplicate in-flight requests and ignore stale responses after rapid tab switching or refresh.
- Provide a visible loading/error state for deferred content and retain a retry path.

**Tests/receipts:**
- Add a browser/JS test or deterministic harness proving initial analytics startup makes no projections/odds request, opening the tab loads both, repeated activation does not issue duplicate in-flight requests, and returning after the refresh interval updates both feeds.
- Record browser Network timing or equivalent endpoint-call trace showing the primary content appears before any projections request; do not claim this as a first-content speedup without a measured before/after improvement.
- Test one feed failing while the other succeeds; verify the available feed renders and the failed feed has a visible retryable error rather than the generic “No data” state.

## Task 2 — Cache expensive projections with safe invalidation (P1)

**Evidence:** `app.py:4780-4804` calls `_served_analytics()`, computes both home and away projections for every returned team, sorts the full result, then returns it. There is no endpoint-level projections cache. Live payload: 687 rows, about 90 KB compressed. Do not start caching against an ambiguous or partially published archive; complete Tasks 4 and 9 first.

**Implementation direction:**
- Add a bounded server-side cache for projection results, reusing the repository's existing cache conventions where appropriate.
- Cache key must account for at least season, live week, and the relevant analytics/model version or freshness identity. An in-process TTL **alone** is insufficient when the D1 archive changes in a different process or container: either validate a cheap durable publish/version marker or bound staleness explicitly and demonstrate that it meets the existing freshness contract. Invalidate on local successful analytics refresh as well. Do not cache errors/empty results as a valid fresh response.
- If concurrent cache misses can trigger duplicate 687-team computations, use a narrowly scoped single-flight/lock pattern; avoid blocking unrelated endpoints.
- Preserve the exact projection values, team universe, ordering, and response shape.

**Tests/receipts:**
- Test cache hit avoids repeating the expensive projection function; test expiry, local refresh, season/week/model changes, and external D1 publication across processes; test simultaneous misses do not corrupt or duplicate work if single-flight is implemented.
- Compare pre-change and post-change JSON values and ordering for the same frozen input; assert exact parity for computed numeric fields.
- Benchmark cold and warm endpoint calls and include status, duration, row count, response bytes, and cache state.

## Task 3 — Reuse analytics serving inputs and avoid serial D1 round trips (P1/P2)

**Evidence:** `app.py:4441-4451` loads the analytics identity map from D1; `_served_analytics()` at `app.py:4475-4480` performs identity read followed by analytics-week/data reads through `load_team_analytics()`; it separately reads team identity rows at `app.py:4514-4521`. `d1_write_path.py:321-338` may query available weeks before reading the selected week. `_served_analytics()` is also called by `/api/projections` and `_build_team_map()`. Optimize against the Task 9 completed-pull read path, not the current `MAX(recorded_at)` query.

**Implementation direction:**
- Measure query count/latency on a cold and warm serving path first.
- Avoid redundant D1 reads by caching identity/team-identity rows with a documented short TTL or version-aware invalidation after successful writes, including **writes by another container**. Reuse assembled numerics within the existing analytics cache only if projections still see the original unmodified data semantics: `/api/analytics` mutates copied rows with composite/record overlays; `/api/projections` currently projects from `_served_analytics()` directly.
- Check `ANALYTICS_FROM_D1` before any D1 read so an explicit opt-out actually avoids D1 latency.
- Where straightforward and supported by the D1 abstraction, combine reads or remove the separate “weeks” query when the requested week is already known. Do not add fragile cross-request state or stale serving without an explicit bound.
- Keep synchronous D1/network work off any async event loop if relevant; preserve normal error fallback.

**Tests/receipts:**
- Add tests asserting the opt-out path performs zero D1 reads and that cached identity data is invalidated/refreshed after both local and externally published archive writes.
- Instrument or wrap the D1 client in tests to assert the expected bounded query count on repeated cache-hit/miss paths.
- Benchmark cold/warm `/api/analytics` and `/api/projections`; report actual D1 call counts and latency.

## Task 4 — Correct append-only analytics snapshot selection (P1 correctness prerequisite)

**Evidence:** `d1_write_path.py:327-338` filters the selected archive with `recorded_at = MAX(recorded_at)` across all team observations for a season/week. `scripts/backfill_fcs_extras.py:119-137` writes `fcs_rating` observations with per-row `_ts()` timestamps. A later partial write can therefore supply the max timestamp without representing a complete analytics pull. In addition, `tests/test_served_equals_d1.py:69-79` reads all matching archive rows and folds duplicates into a map without selecting the exact served snapshot.

**Implementation direction:**
- Introduce or reuse an explicit coherent snapshot/batch identifier for complete analytics pulls. Select the newest *complete analytics pull*, not the max timestamp across unrelated writers or arbitrary rows. Poll/backfill observations must not replace the full numeric analytics snapshot. Coordinate selection with Task 9 so a partially written pull is never published for serving.
- If schema evolution is required, supply a reviewed migration and rollback plan; do not silently repurpose `recorded_at` or change archive retention semantics.
- Update parity tests to select the same exact snapshot that serving selects. Include a regression case: write a complete analytics snapshot, then a later partial `fcs_rating` poll for the same week; serving must still retain all full-snapshot metrics and teams.
- Preserve intended append-only history and current API shape.

**Tests/receipts:**
- TDD regression test must fail on current behavior and pass after the fix.
- Add tests for multiple complete pulls in one week, partial later poll rows, same-timestamp edge cases, and legacy/empty-D1 fallback.
- Show parity against the exact served snapshot and report selected snapshot ID/timestamp plus row/team counts.

## Task 5 — Add and validate an index for the analytics archive read (P2)

**Evidence:** The append-only query in `d1_write_path.py:332-338` filters by `season`, `week`, and `subject_type`, and chooses max `recorded_at`. The checked-in `d1/schema.sql:34-39` has indexes beginning with `(subject_type, subject_id, season, stat_key, week)` and `(season, stat_key, week)`, neither of which directly matches the serving query's filter/order pattern.

**Implementation direction:**
- Use SQLite/D1 `EXPLAIN QUERY PLAN` on the corrected Task 4 query and representative archive sizes before choosing an index. Consider a composite index aligned with the final snapshot query (candidate only: `(season, week, subject_type, recorded_at)`); do not add an index based only on intuition.
- Document write amplification/storage cost and confirm existing backtest queries are not materially regressed.
- Prepare migration and rollback instructions, but do not apply to production without authorization.

**Tests/receipts:**
- Include query-plan output before/after and reproducible archive row counts.
- Benchmark serving-query latency with representative historical volume; verify returned row counts and values are unchanged.

## Task 6 — Make append-only writer mode changes safe for already-running processes (P2 reliability)

**Evidence:** `d1_store.py:324-339` caches `_STAT_OBS_APPEND_ONLY` process-wide until explicit `refresh=True`. `scripts/migrate_stat_obs_append_only.py:111-123` refreshes only its own process after changing the index. An application process that previously detected legacy mode can retain stale mode after the migration.

**Implementation direction:**
- Make active writers recover safely from a stale conflict target or detect mode changes with a bounded strategy. On a conflict-target/index mismatch, refresh mode and retry once only when the failure is positively identified; do not broadly retry arbitrary write failures or risk duplicate writes.
- Keep migration order safe and document how old/new app instances behave during transition.

**Tests/receipts:**
- Simulate a long-lived writer that first detects legacy mode, then the schema switches to append-only. Verify the next write refreshes and succeeds exactly once.
- Verify ordinary D1 failures are not retried as schema mismatches and are surfaced/handled under existing behavior.

## Task 7 — Render only the active Analytics view and debounce search (P2)

**Evidence:** `analytics.html:516-535` rebuilds summary and every metric table on every render, including `renderProjections()`. Search triggers `renderAll` directly on every keystroke at `analytics.html:546-547`; tab switching only toggles classes at `analytics.html:538-544`.

**Implementation direction:**
- Render the active metric section on tab activation and only rerender the affected visible section when search changes. Keep summary/current data accurate and ensure switching tabs renders current filtered results.
- Debounce the search input modestly (e.g. 150–250 ms) without changing filter semantics or delaying visible feedback noticeably.
- Coordinate with Task 1 so deferred projections still initialize correctly and are not rerendered on every analytics keystroke.

**Tests/receipts:**
- Add DOM/browser tests for tab switch, filtered search, empty search, and projections visibility/loading.
- Record render counts or duration before/after on the same 687-team fixture. Confirm no tab becomes blank or stale.

## Task 8 — Parallelize independent Schedule startup requests (P3)

**Evidence:** `schedule.html:705-725` fetches `/api/schedule/current-week` (except when `?week=N` is supplied), awaits it, then awaits `loadWeeks()`, then fetches the selected schedule. The first two are independent only on the no-override path; selection/fallback must still happen after weeks load.

**Implementation direction:**
- Start current-week and available-weeks requests concurrently **only when no forced `?week=N` is supplied**. With an override, do not fetch current-week at all. Preserve the existing fallback to the first available option (the old comment says “nearest” but the implementation uses `sel.options[0]`); do not silently change selection semantics.
- Handle either request failing independently; do not let one failed request prevent the other path from rendering a usable schedule. If weeks cannot be loaded, keep a valid current/forced-week fallback rather than defaulting blindly to the empty dropdown's week 1.

**Tests/receipts:**
- Add deterministic tests for both requests succeeding, current-week failure, weeks-list failure, forced `?week=N`, and unavailable-week fallback.
- Measure startup request sequence and verify the independent calls overlap; confirm selected week and schedule contents match pre-change behavior.

## Task 9 — Publish complete analytics pulls atomically before serving them (P1 correctness; added on review)

**Evidence:** `d1_write_path.py:283-290` builds a full analytics pull, then `d1_store.py:416-437` writes it in many independent D1 statements, up to 400 observations per statement. `app.py:4534-4553` writes numerics first and team/string identity in separate operations; `d1_write_path.py:327-338` selects by the newest timestamp without a committed-pull marker. A mid-write reader or failed later batch can therefore see a partial pull even if unrelated poll rows are excluded (Task 4). `app.py:592-620` and `app.py:4105-4143` can then replace a serving cache after an archive failure.

**Implementation direction:**
- Establish a durable completion/publication marker for a full numeric pull (batch identifier, season, week, expected and confirmed row/team counts, completion time) **only after every chunk has confirmed**. Serve the last completed pull while a new one is in progress; a failed write must leave the previous complete pull selected. Make retries idempotent without accepting a partial marker.
- Decide and document how team/string identity corresponds to the same pull; do not publish a new numeric snapshot against mismatched identity. If publishing identity and numerics together requires staging or an atomic pointer switch, submit a migration and rollback plan rather than hiding partial states behind a TTL.
- Preserve the existing serveable disk fallback when D1 is absent/disabled. Make both scheduled and admin-triggered refresh paths respect the same completion rule before replacing a cache; do not cause a new third-party pull on an ordinary GET.

**Tests/receipts:**
- Inject failure after a confirmed first chunk; assert no partial batch is selected by `_served_analytics()` or `/api/projections`, and the previous complete snapshot still serves. Then resume/retry and assert the new batch is selected exactly once with matching team/metric counts and identity.
- Probe during an in-progress multi-chunk write with a separate reader/process; assert readers see only the old complete pull or the new complete pull, never a mix. Record marker ID, expected/confirmed counts, selected ID, and a read-back from the D1 test database. No production write without approval.

## Final verification and CTO handoff

Before requesting QA verification:

1. Run targeted tests for every task and then `python -m pytest -q`; include exit codes and **pass, skip, xfail, xpass** counts. A skipped D1 parity gate is not a pass. Add frontend/browser tests to the documented test command if they are not included in pytest.
2. Run static checks/build checks used by this repository.
3. Compare API schema, team counts, ordering, and projection values against a frozen baseline; no model/data-semantic drift is allowed.
4. Re-run latency probes on the *candidate implementation* in a local/staging environment for `/api/rankings`, `/api/analytics`, and `/api/projections` with repeated samples. Report median and range, request bytes, status, row counts, and cold/warm distinction. Read-only live probes may establish the current production baseline, but **cannot** verify a candidate that has not been deployed. Do not claim a production speedup based on local timings or one request.
5. Record the serving D1 query plan, calls per request, exact selected **complete** snapshot identity, expected/confirmed/served row counts, and schema migration/rollback steps if applicable. Read back the candidate publication state from an approved D1 test database, not production.
6. Check `git status` and ensure unrelated user data (`data/line_history.json` and any newly changing runtime data such as `data/record.json`) is neither reverted nor included in the implementation commit.
7. Do not deploy. Hand QA the commit SHA, changed-file list, test/benchmark receipts, and any migration approval request for independent re-run.

## Definition of done

- All required correctness regressions and speed-path tests pass.
- The append-only served dataset remains complete after unrelated partial writes.
- Analytics first content remains usable without waiting for projections (as it is today), and the hidden Projections & Odds tab generates no load-time request until opened.
- Warm projection requests avoid recomputation and show a measured improvement with exact response parity.
- D1 query count and/or latency is measurably reduced without violating freshness behavior.
- Only completed, coherent numeric-and-identity analytics pulls can be selected; interrupted archives leave the prior complete pull serving.
- Schedule requests overlap without changing week selection or fallback behavior.
- No changes to the model or betting/data semantics; no production deployment performed. QA independently reruns the candidate and owns the release verdict before any approved production change.
