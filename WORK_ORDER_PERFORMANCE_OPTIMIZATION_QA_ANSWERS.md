# QA Rulings — Performance Optimization Work Order

**To:** CTO  
**From:** QA (independent verification)  
**In response to:** `WORK_ORDER_PERFORMANCE_OPTIMIZATION_QA_QUESTIONS.md`  
**Reference baseline:** `main` at `9e0fba8d7f0f29009a33c6fa1497991f65566484`  
**Scope:** Decisions for implementation and QA acceptance, **not** approval to deploy, write production D1, or migrate production schema.

## Q1 — D1 test bed

Choose **option 1 for implementation**: a hermetic, in-process SQLite harness loaded from the applicable D1 schema/index state. Patch the D1 query/query_full boundary, not the analytics selection logic being tested. Cover real SQL selection, index-mode detection, chunked write interruption, marker publication, reader isolation, and read-back. **Yes: all new Tasks 4/6/9 regressions must run without credentials**; the existing D1 parity gate may remain separately token-dependent and read-only, with its skipped status reported explicitly.

SQLite cannot establish Cloudflare D1 REST error-text, metadata, request-limit, or retry behavior. Before QA can **verify those D1-specific claims**, use a separately authorized scratch D1 database and scoped credential for read-back/failure tests. This is a verification gate, not a prerequisite for starting hermetic implementation. Do not write to production for a receipt. If Jeff declines a scratch D1 test bed, label D1-specific behavior **UNVERIFIABLE**, not passed.

## Q2 — Severity and survival guard

**P1 correctness risk; escalate promptly.** The present append-only reader filters to the maximum `recorded_at` for the week. A later single-row team write can become the entire selected numeric set, making other metrics disappear; downstream composite imputation can conceal it. This is a demonstrated code-path risk, **not a confirmed live P0 outage**. Escalate to P0 if a live read-back proves the served current-week numeric snapshot is incomplete or materially wrong for visitors.

The red/green regression must check **all expected teams and metric keys**, with their values, after a later partial poll—not just that the correct stamp was selected. The completed-publication marker is the source of selection, but validate its expected row/team counts and required key coverage before accepting it. On an absent, malformed, or incomplete marker/selection, serve the previous verified complete pull or the documented disk fallback; never silently promote a partial pull or fill missing metrics merely to make counts look plausible. Define how that fail-safe behaves during first rollout when older data is unmarked (Q6).

## Q3 — Raw analytics cache

**Yes:** a separate cache of raw `_served_analytics()` inputs is appropriate, keyed by the complete publication identity, season/week, and relevant model version. Do **not** project from `_analytics_cache` or the `/api/analytics` response; those rows include composite enrichment and live record overlays. Treat disk-fallback state as a distinct cache identity, and avoid returning a mutable cache object that callers can change. Test exact projection JSON, values, ordering, and team-universe parity against a frozen pre-change input.

## Q4 — Index and query-plan receipts

**Yes:** Task 5 can end with **no new index** if the corrected, marker-based selection query performs adequately at representative archive volume and the query plan supports that conclusion. The work order's candidate index was only a hypothesis for the old shape; do not install it by default.

A **read-only** `EXPLAIN QUERY PLAN` against production D1 is an acceptable plan receipt with authorized read access; it does not authorize DDL and is not a latency benchmark. Pair it with representative-volume local/scratch timings, matched result counts/values, and an assessment of any proposed index's write/storage cost. If read access is unavailable, state that production-plan verification is missing.

## Q5 — Stale writer mode and retry

Recognize a **specific conflict-target mismatch**, such as SQLite's `ON CONFLICT clause does not match any PRIMARY KEY or UNIQUE constraint` or the equivalent specific D1 error. Do **not** classify every error mentioning “conflict” as a mode change. Refresh the detected live index mode and retry the **whole original bulk write at most once**, preserving the same recorded-at/pull stamp and inputs. Never retry arbitrary failures under this rule.

A chunked operation may have partly landed before failure. The retry is acceptable **only if** its SQL is proven idempotent for the actual index state. In append-only mode, `DO NOTHING` for already-landed chunks may report zero new writes; zero new writes on replay does not mean the completed batch is missing. Read back the exact expected keys, values, stamp, and count before publication. If the provider reports ambiguous or incompatible behavior, fail closed—do not publish. Exercise failures before the first chunk, after an early chunk, and on unrelated D1 errors in the hermetic harness; use scratch D1 for provider-specific claims.

## Q6 — Atomic identity and numeric publication

**Yes, prefer one `app_state` value** containing season, week, pull stamp, expected/confirmed counts, publish time, and the matching string-identity blob; publish it last in one confirmed upsert, then read it back. This is a single *publication-pointer* switch, **not an atomic multi-statement archive write**: earlier numeric chunks remain invisible to the serving selector until the pointer is valid. Check the serialized blob against D1 request/value constraints on a scratch database. Keep `teams`-table identity-only seeding explicit and test how a lagging `teams` write affects the served universe.

Before implementing, document transition rules for existing `analytics_identity` and unmarked historical observations. Do not label an arbitrary `MAX(recorded_at)` historical set complete just to bootstrap. If no verified prior publication exists, use a clearly identified, serveable fallback until a complete new pull is published or an approved backfill establishes counts and identity. Moving legacy identity reads behind the marker must not silently erase string fields or break the explicit D1 opt-out. Any production backfill or publication write needs Jeff's separate approval.

## Q7 — Warm projections marker read

**Yes:** one small D1 marker read per warm `/api/projections` request is an acceptable price for exact cross-process invalidation. Do not hide the check behind a TTL without a separately approved and measured staleness bound. Measure its latency and D1-call count, and verify the warm path avoids both the full analytics assembly and the 687-team projection computation. Handle missing/unreadable markers by an explicit fallback policy; a failed freshness check must not silently return indefinitely stale cached projections.

## Q8 — Frontend test receipts

**Yes:** a `node --test` intercepted-fetch trace showing request order, timestamps, and the DOM visibility state is sufficient for the *call-order regression*. Wire the JS suite into a documented test command and report its exit status. It is **not sufficient as the only UI acceptance receipt**. QA will also load the candidate's served page in a local browser and inspect live DOM/network state: tab activation, hidden-tab non-fetch, independent odds/projections failures, visible retry/error, refresh after the interval, and the source badge. No screenshot counts as proof. No production page change is authorized by this test plan.

## Q9 — Deliverable hygiene

- **Yes:** commit the work order and these Q&A documents in a separate `docs:` commit as an audit trail. Do not present questions/rulings as evidence that implementation passed. Do not overwrite the original CTO questions; append a pointer if desired.
- **Yes:** put JS tests under `tests/js/` and document the runner in `OPERATIONS.md`.
- **Yes, cautiously:** correct the contradictory v56/v62 `CURRENT STATE` text, preserving historical verification dates as history. Do not manufacture a current “deploy verified” line: a live read-only `/api/health` response of HTTP 200/build v62 is not a complete container-swap verification. If changing the canonical data-flow docs is required by repo policy, update them accurately, distinguishing observed state from unverified assumptions.

## Q10 — `data/line_history.json`

Leave the pre-existing modification alone. Do not investigate or stop a local writer under this work order unless it demonstrably interferes with testing. Never revert, stage, or commit this unrelated file.

## C1–C3 — CTO corrections

- **C1 accepted:** Tasks 4 and 9 are **one** publication-and-selection mechanism with two independently testable requirements: reject unrelated partial observations and never serve an in-progress/failed full pull. No competing markers or separate publication designs.
- **C2 accepted:** Implement in dependency order **4+9 → 6 → 2 → 3 → 5 → 8 → 7 → 1**. The existing task numbers are identifiers, not an execution order. Keep the three proposed implementation commits (correctness, serving performance, frontend), plus a separate documentation commit if desired.
- **C3 accepted with severity qualification:** change the risk description to “a single later team observation may displace the entire selected numeric payload for that season/week.” **P1 pending evidence of live visitor impact; P0 if confirmed materially wrong live output.**

**C4–C8:** Accepted. On archive failure do not advance a D1-authoritative serving cache or report a successful D1 publication; preserve the prior completed pull, and distinguish any local disk fallback. Name the `ANALYTICS_FROM_D1=0` ordering bug explicitly and test zero D1 reads. Do not confuse raw inputs with the mutated analytics response. Keep Schedule's current `options[0]` fallback semantics and leave the already-parallel best-bets/line-moves calls alone. Analytics already reveals primary content before projections: this task is **not** a first-content fix; ensure the primary fetch owns the source badge so deferral leaves it accurate.

## Verifiability stamp / handoff gate

Hand QA the commit SHA(s), changed-file list, targeted **red-then-green** tests and full suite exit codes with passed/skipped/xfail/xpass counts, an exact completed marker/stamp and expected-versus-read-back team/key/value counts, interrupted-write and stale-writer retry receipts, projection response parity, JS harness output plus local-browser DOM/network trace, and cold/warm timings with D1 call counts. Include any prepared—but **unapplied**—migration/rollback steps. QA independently reruns the candidate; the CTO does not grade its own work.

**Current evidence, not a release verdict:** At the time of these rulings, QA's read-only live requests returned HTTP 200 from `/api/health` (reported build `v62`) and `/api/analytics` (687 teams). Those receipts show the endpoints responded; they do not establish full metric completeness or that the proposed implementation works. **No deploy, production D1 write, or production migration under this work order.**
