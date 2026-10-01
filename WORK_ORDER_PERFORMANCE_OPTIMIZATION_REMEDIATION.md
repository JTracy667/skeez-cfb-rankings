# CTO Remediation Order — Performance Branch QA Blockers

**To:** CTO  
**From:** QA (independent verifier)  
**Repository:** `C:\Users\jtracy\dev\cfb-power-rankings`  
**Candidate branch named in CTO handoff:** `perf/d1-snapshot-safety`  
**Scope:** Fix reproduced branch defects, contain and safely resolve local analytics-file damage, then submit a clean candidate for independent QA.  
**Release status:** **QA VETO — do not deploy or apply the prepared production index migration.**  
**This order authorizes code fixes and isolated tests only. It does not authorize production D1 writes, migrations, config changes, deployment, or destructive local-data restoration.**

## 1. Stop-the-line: protect local data first

During QA, a delegated probe mistakenly invoked `run_weekly_analytics_pull()` and overwrote the working-tree `data/cfbd_analytics.json`. QA takes responsibility for the mistake. Do not rerun that pull or any test/script that writes to repository `data/` until the file has been safely recovered or deliberately quarantined.

QA read-back of the current file: **73 bytes, one row (`Team`, `team_id: 1`, `sp_plus: 30`), SHA-256 `8fa519482d1c5c399ed806998e87189b72aaedf15b417232ca112756754e94d1`**. Its working-tree diff against HEAD deletes approximately 77,400 lines. This hash identifies the damaged current file; it is not a backup.

Required actions:
1. Preserve the current file exactly as evidence, without overwriting it. Record its hash and the repository status before any recovery attempt.
2. Search only for an authoritative pre-probe copy: a user backup, editor/local-history snapshot, filesystem backup, or other known preserved copy. Compare candidate provenance, row count, key values, and hash before restoration. Do not assume the committed version is the user's desired working copy.
3. **Do not run `git checkout`, `git restore`, reset, or a new weekly data pull** on `data/cfbd_analytics.json` or any other modified `data/*.json`. If no authoritative copy can be proven, stop and report that recovery requires Jeff to identify/approve the source. Do not silently substitute HEAD or regenerate data.
4. Preserve all other modified data files, including `data/line_history.json`, `data/line_movements.json`, `data/rating_vintages.json`, and `data/record.json`. They are outside this remediation and must not be staged, reverted, or included in a code commit.
5. Make tests hermetic: copy fixtures into a temporary directory/database and redirect every writer there. Demonstrate tests leave repository `data/` hashes unchanged.

## 2. P1 — publication must mean a complete analytics pull

### Reproduced defect

A partial call through `snapshot_team_analytics()` can publish itself as the complete pull. The adversarial probe wrote a complete two-team/two-metric pull (4 rows), then a later one-team/one-metric pull (1 row); the latter replaced the publication marker and serving dropped the other team and metric. The lower-level partial-poll test does not exercise this publishing entry point.

### Required fix and regression gate

- Trace every writer that can reach the publication function. Only the authoritative full-pull coordinator may publish a full-pull marker. A partial team/metric call, poll, backfill, replay, or helper must never create or advance that marker.
- Persist the expected team set/count and required metric-key set/count (or an equivalently strong manifest) from the full-pull input. Validate exact expected `(team, metric, stamp)` keys and values by read-back; row/team counts alone are insufficient.
- Publish the pointer only after every numeric chunk, identity component, and required read-back succeeds. An interrupted or failed pull must leave the previous verified publication selected.
- Add a hermetic red→green regression through the **same public writer/coordinator path used by production**: publish a complete two-team/multi-metric pull, then invoke a one-team/one-metric partial write and prove the old marker and every old team/key/value remain selected. Then run a complete new pull and prove it switches exactly once.
- Add malformed/missing marker, missing key, wrong value, duplicate/replay, same-stamp replay, and partial chunk failure cases. Fail closed to the previous validated publication or a documented disk fallback; never impute missing data to pass validation.

## 3. P1 — missing/invalid marker must not erase disk fallback

### Reproduced defect

When `analytics_identity` is nonempty but the publication marker is missing/invalid, `_served_analytics()` suppresses the disk data before marker validation and can return an empty numeric dataset. A probe with a disk team (`sp_plus=30`) and a live identity map returned `[]`.

### Required fix and regression gate

- Refactor fallback selection so identity presence alone cannot suppress valid numeric disk data.
- Add tests for: valid marker; absent marker; malformed marker; marker pointing to incomplete rows; empty identity; nonempty identity; D1 unavailable; and `ANALYTICS_FROM_D1=0`.
- Assert exact disk team/metric values are served on the documented fallback path, and assert the opt-out makes zero D1 reads.
- A truly empty/invalid source must produce explicit degraded/error state; do not report successful publication or cache an empty response as fresh.

## 4. P1 — define and enforce numeric/identity consistency

### Reproduced defect and design conflict

The CTO handoff says the publication record contains identity, but serving uses the separate live `analytics_identity` key. Therefore numeric data and the identity blob in the marker do not switch together. Conversely, serving only a frozen marker identity can stale fields the application expects to update live (e.g. streak/movement). Do not claim a fully atomic identity switch while serving identity from an independent key.

### Required decision, implementation, and tests

- Write down the data contract before editing: identify which identity fields are required to interpret/key the numeric snapshot (at minimum stable team identity and matching team universe) and which are intentionally live overlays (if any). Do not blur the two classes.
- The published snapshot must include the identity needed to interpret its numeric rows, or the reader must validate a versioned identity reference that cannot mismatch the numeric publication. Live-only overlay fields may remain separate only if explicitly excluded from the atomic snapshot contract, versioned/freshness-labeled, joined by stable team ID, and proven not to alter numeric selection or universe.
- Add tests that force publication and live-identity writes to succeed/fail independently, prove no mixed numeric/identity team universe is returned, and prove volatile identity fields still refresh as intended.
- Make legacy/unmarked-data behavior explicit. Do not choose `MAX(recorded_at)` as an implicit bootstrap. Until a verified publication exists, use the tested fallback and explicit degraded state.
- Check identity blob serialized size against the provider limit in hermetic and scratch D1; confirm the publication record with an exact read-back.

## 5. P2 — exact replay verification and row validation

### Reproduced gaps

- `d1_store.py` accepted a zero-write replay based on a slice count: after storing value `1.0`, replaying the same stamp/keys with value `99` was counted satisfied while storage remained `1.0`.
- The serving validator accepted a changed `sp_plus` value (`30`→`999`) under the old marker because team/row counts were unchanged.

### Required fix and regression gate

- For idempotent retry, verify the exact expected key set **and each stored value** against the original input and same pull stamp. A count match is not enough.
- Detect missing, unexpected, duplicate, stale-stamp, and value-mismatched rows. Do not advance publication on any discrepancy.
- Preserve the narrow conflict-target-mismatch retry rule: refresh writer mode and retry the original batch at most once, with the same stamp; never retry unrelated D1 errors.
- Add red→green tests for changed-value replay, missing/extra keys, partial first attempt, retry with `DO NOTHING`, wrong team/key count, and ordinary provider failure. Scratch D1 receipts must show original error classification, exact read-back, and no duplicate publication.

## 6. P1/P2 — projections cache must represent the input it caches

### Reproduced defect

The `"disk"` cache identity does not distinguish disk contents. A probe changed fallback projected score from 20 to 55 and the second `/api/projections` response still returned 20 without reloading. Empty projection results are also cached.

### Required fix and regression gate

- Either do not cache projections served from disk fallback, or key/invalidate them using a robust identity for the actual fallback input plus relevant live overlays. Never use a constant `"disk"` key as if it were a version.
- For D1-backed results, validate the completed publication identity and all other live inputs that affect projections; include season/week/model version and appropriate freshness identities.
- Do not cache errors, malformed results, or empty results as valid fresh projections.
- Test same-marker live identity changes, disk input changes, empty→populated transition, marker change, season/week/model change, expiry, and concurrent miss. Assert full response/value/order parity against an uncached computation.

## 7. High — Analytics auto-refresh calls a removed function

### Reproduced defect

`analytics.html:240–243` defines `reloadAnalytics()` to call `fetchProjectionsAndOdds()`, but the page now defines `loadProjectionsAndOdds()`. Timer/visibility refresh can throw `ReferenceError` after the analytics request.

### Required fix and regression gate

- Update timer and visibility refresh to call the current independent-feed loader under the intended active-tab rules. Do not reintroduce a hidden-tab startup fetch or a combined failure path.
- Exercise the actual timer and `visibilitychange` handlers with intercepted fetches and live DOM. Assert no `ReferenceError`, no duplicate in-flight feed fetch, no hidden-tab fetch until required, and independent recovery when one feed fails.
- Verify the source badge describes actual source/status for analytics, projections, and odds; do not label all results `cfbd` unconditionally or retain an obsolete odds count after a failed refresh.

## 8. Medium — Schedule week-list failure must preserve resolved week

### Reproduced defect

If `/api/schedule/weeks` fails, real HTML's initial Week 1 option remains. With `/schedule/current-week` resolving to 9, startup selected/fetched Week 1. A test using an empty select stub misses this.

### Required fix and regression gate

- Distinguish “weeks request succeeded with options” from “weeks request failed and markup still has its default option.” On failure, preserve the forced or resolved current week; do not interpret the static Week 1 placeholder as the first published week.
- Test against actual page markup for current week 9 + weeks HTTP 500, fetch rejection, forced `?week=9`, successful list containing the week, and successful list that omits it (retain the documented first-available fallback only for that last case).
- Read back selected dropdown value, requested schedule URL, and rendered week label/cards. Preserve existing selection semantics where the list is genuinely available.

## 9. Audit CTO's existing claims; do not promote them to acceptance

The handoff's own statements conflict with QA's repros and with the prior authorized scope. Resolve each with reproducible receipts, not explanations:

- The handoff says partial-poll survival, complete publication, identity consistency, projection invalidation, and frontend behavior are verified. The repros above refute those specific claims; revise the handoff after fixes and retain the original as historical evidence.
- The handoff says a production publication-marker write occurred and the index should be applied alongside deployment. The work order prohibits production D1 writes/migrations/deploy absent Jeff's separate approval. This QA order grants none. Record the reported write for review, but do not write to production, apply DDL, or deploy.
- A live v62 response (health 200; analytics 687 teams) is only an observation of v62, not evidence the candidate branch is correct or deployed.
- D1 provider-specific claims remain unverified by QA until independently repeated on an authorized scratch D1. Keep tests credential-free and hermetic as the main regression suite; report scratch-D1 skips explicitly, never as passes.

## 10. Safe implementation and test workflow

1. Check branch, commit, staged/unstaged diff, and all `data/` hashes before editing. Do not assume a clean tree. Preserve unrelated user data.
2. Recover/resolve the damaged local analytics fixture before invoking any mutating data scripts; if no authoritative copy can be proven, stop that part and report to Jeff. Use isolated fixture copies for tests.
3. For every defect above, write a focused test first, run it to see the expected failure (RED), implement the minimal correction, then rerun to GREEN. Do not weaken, skip, or rewrite assertions to obtain a pass.
4. Execute targeted regressions and the complete documented suite from a clean fixture. Report exact command, exit code, passed/failed/skipped/xfail/xpass counts, duration, and fixture path. Confirm repository data hashes unchanged after tests.
5. Run Node tests and the live-page harness against a local candidate server; read DOM/network/error state (not screenshots). Cover timer/visibility and real markup failure cases.
6. Compare frozen input/output JSON: team universe, required fields, ordering, and every numeric projection must remain exactly unchanged. No model formula/constants/source semantics changes.
7. Benchmark cold/warm APIs with repeated samples, status, row count, bytes, D1 calls, and p50/range. No speedup claim from a single request or production v62 observation.
8. Re-run scratch-D1 tests with exact marker read-back, key/value/count reconciliation and provider-error behavior if authorized scratch credentials are available. Otherwise mark those provider-specific items **UNVERIFIABLE**.
9. Submit commit SHA(s), changed-file list, test/benchmark receipts, current repository status, migration/rollback plan (if any), and corrected handoff to QA. Do not deploy.

## 11. QA acceptance checklist

QA will not lift the veto until all are true:

- [ ] The local `data/cfbd_analytics.json` is restored only from an authoritative preserved copy, or its unrecoverable state is explicitly reported to Jeff; no other user-modified data was overwritten/staged.
- [ ] Partial calls/polls cannot advance full-pull publication; later partial write leaves every prior expected team/key/value served.
- [ ] Publication validates exact key/value manifest, not only row counts; retries are idempotent and same-stamp.
- [ ] Missing/incomplete marker fallback serves correct disk/previous-complete data and reports degraded state; D1 opt-out issues zero D1 reads.
- [ ] Numeric snapshot and required identity are coherent; volatile overlays have explicit scope, version/freshness, and tested joins.
- [ ] Projection cache invalidates on every input change and never treats an empty/error/fallback as a durable valid cache without an exact input identity.
- [ ] Analytics timer/visibility refresh has no undefined function and handles independent feed failures correctly.
- [ ] Schedule list errors retain forced/current week and are tested against real markup.
- [ ] Hermetic full suite passes with honest skip counts; tests do not mutate repo `data/`.
- [ ] Candidate browser DOM/network checks, exact parity, cold/warm timings, and scratch-D1 receipts (or explicit UNVERIFIABLE) are supplied.
- [ ] No production write, migration, config mutation, or deployment occurred without Jeff's separate explicit authorization.

**Owner:** CTO fixes and supplies candidate receipts. **QA owns the independent rerun and final verdict.**
