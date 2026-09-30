# QA Questions — WORK_ORDER_PERFORMANCE_OPTIMIZATION.md

**From:** CTO
**To:** QA
**Date:** 2026-09-30 (PT)
**Repo:** `C:\Users\jtracy\dev\cfb-power-rankings` — review baseline `9e0fba8` (matches the WO), working tree = `data/line_history.json` modified (pre-existing, preserved) + the WO file untracked
**Status:** not started. Answers below are required before implementation. No deploy, no production write, no migration will be applied under this WO.

---

## 1. What I verified in the WO (everything checks out)

Every line reference in the work order resolves against the current tree:

| WO claim | Verified |
|---|---|
| `analytics.html:557-560` awaits `fetchAnalytics()` then calls `fetchProjectionsAndOdds()` | yes |
| `fetchProjectionsAndOdds()` is `Promise.all([/api/projections, /api/odds])` — one failure rejects both | yes (`analytics.html:569-586`) |
| `renderAll()` rebuilds all 15 sections; search calls it per keystroke; tabs only toggle classes | yes (`analytics.html:516-535`, `547`, `538-544`) |
| `app.py:4780-4804` — `/api/projections` has no endpoint-level cache, 687 teams × 2 projections per call | yes |
| `d1_write_path.py:332-338` selects `recorded_at = MAX(recorded_at)` | yes |
| `d1_store.py:324-339` caches `_STAT_OBS_APPEND_ONLY` process-wide | yes |
| `d1_store.py:416-437` writes a pull as many independent statements (≤400 rows each), no completion marker | yes (~60 statements for a ~22k-row pull) |
| `app.py:592-620` / `4105-4143` replace the serving cache without checking the archive result | yes (`_store_team_analytics` returns 0 on failure; `_cache_set` runs regardless) |
| `schedule.html:705-725` serial current-week → weeks → schedule | yes |

Confirmed live defects the WO did **not** cite (both real, both cheap to fix):

- **`ANALYTICS_FROM_D1=0` does not avoid D1 latency.** `_analytics_identity_map()` (a D1 read) runs at `app.py:4475`, but the opt-out check is at `4477` — the read happens before the flag is read. Task 3's requirement is a one-line reorder plus a test.
- **`/api/analytics` response cache is already 10 min** (`ANALYTICS_TTL=600`, `app.py:1369`), and it stores **mutated** rows (composite enriched + live W/L overlaid). `/api/projections` has no cache and is ~4 D1 round trips per call inside `_served_analytics()` (identity read → weeks query → main query → `teams` identity rows).

---

## 2. Decisions Jeff has already made (so QA knows the shape)

- **Q2 (Tasks 4 + 9 mechanism):** D1 `app_state` publication marker. One record per (season, week): the `recorded_at` stamp of the completed pull, expected/confirmed row and team counts, identity stamp, published_at. Written **last**, as a single atomic upsert. Serving selects the marker's stamp instead of `MAX(recorded_at)`. **No change to `stat_observations`, no table rebuild.**
- **Q3 (frontend tests):** `node --test` (built into Node 26, zero new deps) with a minimal in-repo DOM/fetch stub, run against a local `uvicorn` on :8003 — not the live site, not jsdom.
- **Q4 (handoff):** one branch, three staged commits — (1) correctness 4/9/6, (2) serving perf 2/3/5, (3) frontend 1/7/8 — each with its own receipts, one handoff to QA. No deploy.

---

## 3. Questions for QA

### Q1 — Test bed for D1 behaviour (blocking; the only thing I need an external decision on)
Tasks 4/6/9 require TDD against real D1 behaviour (the regression test must FAIL on current code first), but the WO forbids production writes **and there is no D1 test database**: `d1_store.py` is a thin wrapper over the Cloudflare D1 REST API pointed at production `cfb-history` (`c3ec3149-…`), with no local mode and no non-prod target.

**Which do you want?**
1. *(my recommendation)* A hermetic in-process `sqlite3` harness: patch `d1_store.query` / `query_full` to a local sqlite3 loaded from `d1/schema.sql`, so SQL semantics, the unique index, chunked writes and `EXPLAIN QUERY PLAN` are all real and reproducible with **no credentials at all**. Prod stays read-only for the existing parity gate.
2. Jeff creates a scratch D1 database + scoped token and I point tests at it (real D1, needs his dashboard action).
3. Both — sqlite for the suite, scratch D1 for the Task 9 read-back receipt.

**Sub-question:** do you require that every new Task 4/6/9 test runs **hermetically with no credentials**, leaving `tests/test_served_equals_d1.py` as the only token-dependent check? (I recommend yes — a test bed that needs prod creds will be skipped in most contexts, which is exactly the "skip read as a pass" trap your WO calls out.)

### Q2 — Severity confirmation: is this P1 or P0?
The WO frames Task 4 as "a later partial write can supply the max timestamp without representing a complete analytics pull". In the current code the consequence is stronger than that. With v62 append-only, serving selects **only** the rows carrying the max `recorded_at` for `(season, week, subject_type='team')`. A single later writer of any team row for the live season does not merely shadow a complete pull — it becomes the whole served numeric payload, so every other metric for that week disappears and the composite silently imputes 50 for the missing inputs.

Live writers that do exactly this, all of which include 2026 in their season lists:
- `scripts/backfill_fcs_extras.py:116-137` (`fcs_rating`, `SEASONS = [2021…2026]`, per-row `_ts()`)
- `massey_fcs.py:262`, `scripts/backfill_unmatched_names.py:80`, `scripts/backfill_d1.py:188,205`

**Confirm:** (a) severity label; (b) that the Task 4 regression test must assert **full-metric survival** (all metrics + all teams still served), not just "the newest complete pull wins"; (c) whether you want a serve-side guard as well as the marker (e.g. refuse to serve a selection that covers only a small fraction of the expected keys), or the marker alone is sufficient.

### Q3 — Task 3 semantics: may projections reuse *inputs* rather than the response?
`/api/analytics`'s `_analytics_cache` holds **mutated** rows (`_enrich_with_composite` adds `*_contribution` fields; `_overlay_records` pushes live W/L onto them). `/api/projections` must project from the unmodified `_served_analytics()` output.

**Confirm:** I add a **separate** cache for the raw `_served_analytics()` result keyed by the publication marker (season, week, marker stamp), and do **not** make projections consume the `/api/analytics` response. Your WO's wording ("Reuse assembled numerics within the existing analytics cache only if…") reads as permission — I want it explicit, because reusing that payload would change the projections response shape (new contribution fields) rather than only speeding it up.

### Q4 — Task 5: is "no new index" an acceptable outcome, and is a prod read-only query plan an acceptable receipt?
With the marker design, the selection query loses the correlated `MAX(recorded_at)` subquery and becomes `… AND recorded_at = ?` — the index candidate in the WO (`(season, week, subject_type, recorded_at)`) was chosen against the *old* shape. Your own instruction is "do not add an index based only on intuition", and the honest answer may be that the existing indexes are adequate for the corrected query.

**Confirm:** (a) Task 5 may legitimately conclude "no index change required" with query-plan receipts; (b) `EXPLAIN QUERY PLAN` issued **read-only** against production D1 counts as an acceptable receipt (it is a read; no write, no migration), or you want it done only on the hermetic/local archive with representative volume.

### Q5 — Task 6: what counts as "positively identified" conflict-target mismatch?
I need a detection rule that cannot be confused with an ordinary D1 failure, because a wrong retry on a bulk write risks duplicate rows. My proposal: catch the specific D1 error text for a missing/incorrect ON CONFLICT target (the migration's own failure signature), `stat_obs_append_only(refresh=True)`, rebuild the statements and retry **once**; any other error propagates under existing behaviour, unretried.

**Confirm:** (a) the acceptable detection string/signature (or "any error whose text names the conflict target"); (b) that a single retry is acceptable for a **multi-statement** chunked write — i.e. the retry must be safe when chunks 1..n already landed (idempotent under both modes, since append-only is `DO NOTHING` and legacy is `DO UPDATE`). My reading is yes, but it is a real duplicate-write question and I want QA's ruling.

### Q6 — Task 9: identity and numerics in ONE atomic write?
Your WO: "do not publish a new numeric snapshot against mismatched identity… If publishing identity and numerics together requires staging or an atomic pointer switch, submit a migration and rollback plan."

With the `app_state` marker I can make identity and the numerics-marker switch together in a **single** upsert: one `app_state` value carrying `{stamp, season, week, counts, identity: {...}}`. That removes the mismatch window entirely with no migration.

**Confirm:** acceptable to move the identity blob into the marker record (the door `_analytics_identity_map()` then reads the marker), or do you want identity to stay in its own `app_state` key with ordering guarantees only? The first is strictly safer; the second is less churn to a documented door.

### Q7 — Task 2: is one small D1 read per warm `/api/projections` acceptable?
Exact invalidation ("validate a cheap durable publish/version marker") costs one single-row marker read per request even on a warm cache — still a large win over 687 teams × 2 projections (~4 D1 round trips and the full compute), but it is not zero D1.

**Confirm:** marker-check-per-request (exact freshness, one tiny read) is the intended reading of "bound staleness explicitly", or you want a short TTL layered on top (fewer reads, but it reintroduces exactly the cross-process staleness your WO warns about).

### Q8 — Task 1/7 acceptance: does a stub-fetch call-order trace satisfy the receipt?
You ask for "browser Network timing **or equivalent endpoint-call trace** showing the primary content appears before any projections request". The `node --test` harness will record every intercepted fetch with a timestamp and the DOM state at that moment (primary content visible, projections section not yet populated).

**Confirm:** (a) that trace is sufficient, i.e. no real-browser receipt is required; (b) the new JS tests are added to the documented test command (I will add a `tests/js` runner and wire it into the repo's test entry point, plus document it in `OPERATIONS.md`).

### Q9 — Deliverable hygiene
- OK to commit the WO file itself (currently untracked) and this Q&A alongside the implementation? (Recommend yes, as the audit trail; separate `docs:` commit.)
- OK to correct `OPERATIONS.md`'s `CURRENT STATE`/"Last verified" line, which still reads **"DEPLOY VERIFIED LIVE: v56"** while the same block says v62? Doc hygiene rules require it, but it is outside the WO's scope.
- New frontend tests under `tests/js/` — any objection?

### Q10 — `data/line_history.json`
It was already modified at WO creation and is still modified now. I will not revert or stage it, per the WO. **Do you want me to investigate why a local process is writing it**, or leave it strictly alone until it is in scope for something?

---

## 4. Corrections / observations for QA to review

**C1 — Tasks 4 and 9 are a single fix, and the WO treats them as two.** Both ask for a completion/publication marker on a complete pull (Task 4: "an explicit coherent snapshot/batch identifier for complete analytics pulls"; Task 9: "a durable completion/publication marker"). Implementing them separately risks two mechanisms disagreeing. Recommend: one marker, one migration, two named acceptance criteria. Jeff has approved the marker design.

**C2 — The WO's numbering contradicts its own dependency text.** Task 2 says "complete Tasks 4 and 9 first"; Task 3 says "optimize against the Task 9 completed-pull read path"; Task 5's index choice depends on the *corrected* Task 4/9 query, yet numbers below both. Proposed real order: **4+9 (one change) → 6 → 2 → 3 → 5 → 8 → 7 → 1**, which is also the commit staging (correctness → serving perf → frontend).

**C3 — Task 4 understates the failure.** See Q2: it is not "a partial pull may be selected", it is "one poll row can blank the entire numeric payload for the week". Please rule on severity.

**C4 — Profile of how a failed archive goes unnoticed today.** `_store_team_analytics` swallows failure and returns 0 (`app.py:4554`); both refresh paths then write the disk file and call `_cache_set` unconditionally (`app.py:605-615`, `4127-4140`). So a failed archive still refreshes the in-process cache, and the staleness only becomes visible after cache expiry. Task 9 mentions this; the fix should be "replace the serving cache only when the publication marker confirms", which is also what makes the marker testable end-to-end.

**C5 — Task 3 has an unnamed one-line bug.** The `ANALYTICS_FROM_D1=0` path still performs a D1 identity read (see §1). Worth naming in the test list since your WO already requires "the opt-out path performs zero D1 reads".

**C6 — Task 2's "reuse the repository's existing cache conventions" should not be read as "reuse `_analytics_cache`".** That store is a response cache of mutated rows (see Q3). Flagging so the expectation is not literal reuse.

**C7 — Task 8's fallback semantics: I will preserve `sel.options[0]`, not "nearest".** Your WO notes the old comment says "nearest" while the code uses `options[0]`, and instructs no silent change. Confirming I keep `options[0]` exactly as-is, and that the new tests assert *current* behaviour rather than the comment's intent. Also FYI: `fetchBestBets()` / `fetchLineMoves()` at `schedule.html:727-728` already fire unawaited in parallel — they are not part of the serial chain and I will not touch them.

**C8 — Task 1 is not a first-content fix, and the WO says so.** Agreed and confirmed against the code: `fetchAnalytics()` already hides the loader and shows `#content` at `analytics.html:227-229` before any projections request. One extra detail: `fetchProjectionsAndOdds()` also overwrites `#sourceBadge` (`analytics.html:581`); deferring it must not leave the badge wrong or empty, so the badge text moves to the primary path.

---

## 5. What I need back

Answers to §3 Q1–Q10 (Q1 especially — it decides the test bed) and a ruling on C1–C3. Then I implement in the staged order above, verify locally, and hand you a commit SHA, changed-file list, test/benchmark receipts, and the prepared (unapplied) migration + rollback for the marker if one is needed. No deploy.
