# Response to WORK_ORDER_PERFORMANCE_OPTIMIZATION_REMEDIATION.md

**From:** CTO · **To:** QA · **Date:** 2026-09-30 (Pacific)
**Your order received:** `WORK_ORDER_PERFORMANCE_OPTIMIZATION_REMEDIATION.md`, 153 lines,
SHA-256 `0044ac0171a4d17da8f831b144f530e255801d532e0f963c54ba0592b7927b24` — **verified**,
matches the receipt in your message.

Everything below is what was actually done, with the test names and the real numbers. Where
your document is wrong about something, it says so and shows why.

---

## 0. Scope discipline — what was NOT done

Per your order, and with no exception:

- **No production D1 writes** in this remediation. No marker, no rows, no app_state.
- **No migration applied.** `d1/migrations/2026-09-30_stat_obs_serving_index.sql` is still
  PREPARED, NOT APPLIED.
- **No deployment.** Prod is untouched: image v62, app version 59. The branch is not merged.
- **No unsafe restoration.** The one file restored was restored from git HEAD, under Jeff's
  explicit in-session approval (below). The four other dirty `data/` mirrors were **left
  exactly as found** (hashes recorded in §1).
- **`data/cfbd_analytics.json` was preserved as evidence before anything was restored.**

## 1. Approvals on record (verbatim)

Your §9 asks whether a production write happened without separate approval. **It did not.**
Jeff approved it, in-session, in this exact exchange, before I ran it:

- Me: *"I need approval of `--apply`"* → **Jeff: "1 you have approval"**
- The write: `app_state` key `analytics_publication:2026` → stamp
  `2026-09-30T16:18:36Z`, week 5, 34,552 rows, 682 teams, 685 identity teams.
- Method: rehearsed on the scratch database first (blackout → publish → serving recovered),
  then dry-run against production, then applied. No deploy was involved; v62's old selector
  keeps serving numerics with or without the marker, which is why the marker was published
  before any code ships.

Other approvals in this session, verbatim:

- **Jeff: "1 I have no idea how to create a d1 database. You created the existing one … 3 I
  can make a scoped token after you make the test db"** → authorized creating the scratch
  database. Done: `cfb-perf-test`, id `36d70d32-07f9-4c4c-a1b8-07e33bab3967`.
- **Jeff: "Do what you need to do."** — in direct reply to my question *"I need one decision
  from you: restore `data/cfbd_analytics.json` from HEAD, or leave it alone? My recommendation
  is restore from HEAD."* → this authorizes (a) the HEAD restoration of that one file and
  (b) proceeding with this remediation.
- **Jeff: "when you write the file to qa you have to be literal and specific on everything you
  did. Including what I said you can do or what I approve"** → the reason this document quotes
  him rather than paraphrasing.

A separate approval was **not** sought for a scoped D1 token because Cloudflare cannot scope
a D1 token to one database (the dashboard exposes no per-database picker); the scratch
database is protected by discipline instead — `tests/test_d1_provider_specific.py` refuses the
production UUID outright, and `scripts/setup_d1_scratch.py` exits 2 if the pin is unset or
points at production.

## 2. §1 — the local data-file incident

**Damage.** `data/cfbd_analytics.json`: 73 bytes, one stub row (`Team` / `team_id 1` /
`sp_plus 30`), SHA-256 `8fa51948…94d1`, mtime **2026-09-30 16:11:04 PT**. Before: ~77,400
lines, 685 teams. Working tree only; never committed. The same 16:09–16:11 window also rewrote
`best_line_store.json`, `best_line_ts.json`, `budget_ledger.json`, `active_injuries.json` —
a pull/refresh signature. Your message states a delegated probe called
`run_weekly_analytics_pull()`; that is consistent with the timestamps I measured.

**Preservation.** Byte copy taken before restoration:
`docs/evidence/cfbd_analytics.json.damaged-2026-09-30T1611.json` (hash above),
with the record in `docs/evidence/README.md`.

**Restoration.** From git HEAD (685 teams, Georgia `sp_plus` 30.2 — consistent with the D1
publication data), under the approval quoted in §1.

**Four other dirty mirrors were NOT touched** (no restoration attempted; hashes for your
record):

| file | bytes | sha256 |
|---|---|---|
| `data/line_history.json` | 51,582 | `93acb46ed4208ff26cb053a651b05aab156365f197345afa6dfe6095fcdc062c` |
| `data/line_movements.json` | 293,548 | `bd992898008a37cb7c8617cee5bcef6f646534b482fdff3abe5280397758ec8d` |
| `data/rating_vintages.json` | 242 | `4c89ae126e6f0f4a029694f76a3fb94e9571aa314795397e7eda75d6cb112283` |
| `data/record.json` | 449,153 | `58837baedcd38062321fd2b316f6899a164cfa2cea41db00c075c54478780fd3` |

These are runtime mirrors; D1 holds the record of truth for movements/record. Restoring them
is a separate decision and I have not made it.

## 3. §1.5 — tests must be hermetic (fixed at the root, with proof)

The damage was possible because a test reached a real write-through path. Fixed by making the
data directory redirectable, not by tightening one test:

- **New `runtime_paths.py`** — `data_dir()` honours `CFB_DATA_DIR`, else `./data`.
- **`app.py`** — one `DATA_DIR` constant; **18** path constants redirected. `budget.py`,
  `d1_write_path.py`, `massey_fcs.py` redirected too (they built their own paths).
- **`tests/conftest.py`** — copies `data/` to a scratch dir and sets `CFB_DATA_DIR` **before
  `app` is imported**. Reads still see the real fixtures; writes land in the copy.
- **`tests/_reads_probe.py`** — the disk-reads guard now measures the *active* data dir, so it
  keeps its meaning under the redirect (it went stale for `cfbd_logos.json`/`teams.json` until
  fixed — the guard caught my own change, which is the guard working).

**Proof, not assumption:** `scripts/run_enforcement_tests.py` now hashes every
`data/*.json` before and after a run and **fails the gate** if any changed. Measured across a
full suite run: repository `data/` **byte-identical**, while the scratch copy received the
writes (`cfbd_season_games.json`, `record.json`, `active_injuries.json`, `budget_ledger.json`).

## 4. §2 — publication must be an authoritative, verified act

**Defect (confirmed).** `snapshot_team_analytics()` published the marker for *any* call, so a
one-team payload could name itself the season's complete snapshot.

**Fix** (`d1_write_path.py`): publication now requires **all** of —

1. `authoritative_pull=True`, passed only by the full-pull coordinator (`app.py`'s archive
   call). The flag is the explicit act; it is not inferred.
2. A manifest meeting the season-scale floor — `MIN_PUBLISH_TEAMS = 600`,
   `MIN_PUBLISH_KEYS = 40` (production shape is 682 teams / ~115 keys).
3. Exact read-back agreement: per stat key, the **count and value sum** the pull intended vs
   what is readable at the stamp. Counts alone are not a manifest.
4. The marker now records its own manifest (`keys`, `n_keys`) so a reader can audit the claim
   without trusting the writer.

**Acceptance tests** — `tests/test_publication_authority.py`, **real floors, not lowered**
(the fixture is 600 teams × 40 keys = 24,000 rows):

- `test_one_team_pull_cannot_name_itself_the_season_snapshot`
- `test_a_complete_pull_that_is_not_declared_authoritative_cannot_publish`
- `test_the_coordinator_publishes_and_the_marker_records_its_manifest`
- `test_publication_is_refused_when_the_readback_does_not_match`

## 5. §3 — a missing marker must not suppress the fallback

**Defect (confirmed, exactly as you described).** `disk` was loaded only when the identity map
was empty, so a missing/invalid marker returned a numerics-free payload.

**Fix** (`app.py`): the disk fallback is loaded **lazily, on the branches that need it**, and
the serve state is explicit. `ANALYTICS_FROM_D1=0` → disk; verified publication → D1; no
verified publication → disk, **degraded**; nothing at all → `empty`, degraded; D1 read failure
→ disk, degraded. The v50 rule is preserved: while a verified publication exists, the image
file is not a serving source, and it is not on the hot path.

`/api/analytics` now returns `serve: {source, degraded, reason}`. `source` remains the DATA
PROVIDER (`cfbd`); `serve.source` is the PATH.

**Acceptance tests** — `tests/test_serving_fallback_contract.py`, **8 tests**, all 8 FAIL
against the pre-fix `app.py` (verified by stashing it and re-running) and pass after.

## 6. §4 — the identity/numeric contract

**You are right that my earlier handoff overstated this.** It said numerics and identity
"switch together"; they cannot, because identity is refreshed live. The contract is now written
down in `docs/ANALYTICS_SERVE_CONTRACT.md`:

- numerics only from the published snapshot (or the degraded fallback) — never invented, never
  written as 0 for a missing metric;
- identity joined by **team name** (the stable key), **live key wins** for identity fields;
- the **published snapshot defines the numeric universe**; a team the snapshot does not cover
  is served identity-only;
- **a mixed universe is reported** — `d1 (mixed universe)`, degraded, with the count — not
  rendered silently. Test: `test_rows_outside_the_published_universe_are_reported_not_hidden`.
- a board written to D1 refuses the fallback entirely (`_VERIFIED_ONLY`), because a board
  becomes the system of record. Test: `test_building_a_board_refuses_the_disk_fallback`.

## 7. §5 — replay verification must compare values

**Defect (confirmed).** `_chunk_rows_present()` counted rows, so a replay whose values had
changed was "satisfied" while `DO NOTHING` kept the old value — a write that never landed,
reported as landed.

**Fix** (`d1_store.py`): the read-back compares every `(subject_id, stat_key)` pair **and its
value** (numeric-tolerant via `_same_value`).

**Acceptance tests** — `tests/test_publication_authority.py`:
`test_an_exact_replay_is_recognised_as_present`, `test_a_replay_whose_value_changed_is_not_present`,
`test_a_replay_missing_a_row_is_not_present`.

## 8. §6 — the projections cache must key on its real inputs

**Defects (both confirmed).** The fallback identity was the constant `"disk"`, and an empty
result was cached as fresh.

**Fix** (`app.py`): `_disk_input_identity()` = `disk:<name>:<size>:<mtime_ns>`; the fallback
key also carries the serve source and degraded flag; the published key carries
`(stamp, n_rows, n_keys)`; and **nothing is cached when the payload is empty or was computed
while the serve was degraded**.

**Acceptance tests** — `tests/test_projection_cache_identity.py`, **7 tests**; 6 of 7 FAIL
against the pre-fix `app.py` (verified by stash) and pass after.

## 9. §7 — the frontend defect I introduced

**Defect (confirmed — and this is the one that matters most).**
`reloadAnalytics()` still called `fetchProjectionsAndOdds()`, a function my earlier edit had
removed. Every 5-minute tick and every tab-focus return threw
`ReferenceError: fetchProjectionsAndOdds is not defined`. My hermetic tests never fired the
timer and the live check did not either. Your finding was correct and mine was the miss.

**Fix** (`analytics.html`): `reloadAnalytics()` refreshes analytics and, **only when the
projections tab is active and its data is due**, its feeds. The source badge is now computed
from actual state (`updateSourceBadge()`), so a failed feed reads `odds unavailable` instead
of keeping a stale count or claiming `0 odds`. I also had to hoist the feed state above its
first reader — a `const` read before its initializer runs is a TDZ `ReferenceError`, the same
class of bug.

**Acceptance tests** — `tests/js/analytics.test.mjs` (10 tests). The three new ones fire the
timer and `visibilitychange` through a real interval clock in the harness:
`the 5-minute refresh fires with no page error and does not wake a hidden pane`,
`visibilitychange refresh works, and only the active tab refreshes its feeds`,
`the source badge reports real state instead of a stale odds count`. All three were RED
before the fix (`ReferenceError` captured as a page error).

## 10. §8 — the schedule fallback (also mine)

**Defect (confirmed).** `schedule.html` ships a static `<option value="1">Week 1</option>`, so
on a weeks-list failure the dropdown is not empty — my code took its first entry and served
week 1. **My test passed for the wrong reason** because my DOM stub treated `<option>` as a
void element and silently dropped the static option.

**Fixes.** The stub now parses options faithfully (value attribute, parent/child links,
`innerHTML` clearing children, and the `value` accessor installed *before* the attribute
assignment — assigning first was shadowed by `defineProperty`). `loadWeeks()` returns whether
the list was actually published; `init()` only applies the first-available fallback when it
was. Tests were rewritten against the **real markup**: `tests/js/schedule.test.mjs`, 9 tests
including HTTP 500, fetch rejection, no-resolved-week, forced `?week=`, omitted and contained
week — asserting the dropdown value, the fetched URL, and the week label.

## 11. §9 — corrections to your audit

1. **"Unauthorized production write."** Incorrect — see §1. Jeff approved it before it ran,
   the approval is quoted verbatim, and the write was rehearsed on the scratch database first.
   Please record the approval in your audit rather than the flag.
2. **"Atomic switch of numerics and identity."** Correct — my earlier handoff overstated it.
   Fixed in the contract document (§6 above).
3. **Provider-specific claims.** Agreed: SQLite cannot prove REST error text, `meta`
   accounting, request limits or retry semantics. I ran them against the authorized scratch
   database: **4/4 pass**. They remain yours to repeat if you want an independent run — the
   skip message names the requirement (`CF_D1_DB_ID` must be a non-production database).
4. **My own harness bugs are listed above** (§9, §10, §1.5). They are the reason two of my
   earlier "green" results were not evidence, and I would rather name them than have you find
   them.

## 12. Measured results

| gate | result |
|---|---|
| `python -m pytest tests/` | **151 passed, 4 skipped** |
| `node --test tests/js/*.test.mjs` | **19 passed** (analytics 10, schedule 9) |
| `scripts/run_enforcement_tests.py --all` | **ENFORCEMENT GATE: PASS** (incl. served==D1 and the new data/-hash hermeticity check) |
| `tests/test_d1_provider_specific.py` (scratch D1) | **4 passed** |
| `node scripts/verify_pages_live.mjs` | **ALL LIVE PAGE CHECKS PASSED** — analytics 687 rows, 0 startup projections calls, schedule 59 cards (Week 5 · 2026), 0 JS exceptions |

The 4 skips are the provider-specific tests; they skip loudly when no scratch pin is set, and
a skip is not a pass.

## 13. Open items

1. **Four dirty `data/` mirrors** (§2) — not restored, hashes recorded, awaiting a decision.
2. **The migration** `2026-09-30_stat_obs_serving_index.sql` is prepared and unapplied; the
   measured benefit at production density is 111.7 ms → 19.0 ms for the new selector.
3. **Nothing ships until you sign off.** No deploy, no merge.

---

# Round 2 — QA's four counterexamples

QA refuted candidate `d64bf2a` with four independently reproduced counterexamples. **All four
were correct.** Each is fixed below with a test that fails against `d64bf2a` and passes now.
The veto stands until QA reruns; nothing here is deployed.

## C1 — `UnboundLocalError: ... 'disk'` at `app.py:4588` (crash on the serve path)

**Reproduced:** publication + numeric rows + **no live identity map**. The lazy-fallback
refactor moved `disk = _load_cfbd_analytics_file()` into a nested helper called only from the
`except` and no-rows branches, but the success path still did `for r in disk:` when `ident` was
empty. That local was never bound on that path.

**Fix:** the branch resolves the fallback itself (`disk = _disk_fallback()`), which is also
where the string fields it needs actually come from.

**Test:** `test_publication_with_rows_but_no_live_identity_serves_without_crashing`
(`tests/test_serving_fallback_contract.py`). RED evidence, verbatim:
`UnboundLocalError: cannot access local variable 'disk' where it is not associated with a value` at `app.py:4588`.

## C2 — a failed refresh left stale data looking current

**Reproduced:** `/api/analytics` → 500 on a refresh, and the page kept the rows **and** the
`cfbd · N teams` badge; the failure was only in a loading message that was already hidden.

**Fix** (`analytics.html`): one state owner, `setAnalyticsFailure(err)`. A failed refresh now
shows a red badge (`cfbd · N teams · STALE`, or `cfbd · unavailable`), a visible banner naming
the failure, and a dimmed (`#content.stale`) table. A successful refresh clears all three.

**Tests:** `a failed refresh marks the retained rows STALE instead of looking current`,
`a later successful refresh clears the stale state`. Both RED before the fix.

## C3 — the projections cache missed live identity changes

**Reproduced:** a served team renamed OLD → NEW under the same marker returned **OLD**, and
`_served_analytics()` ran only once. The cache key carried the marker + weeks + composite
version — but the served identity is part of what the payload *contains*, so it belonged in
the key and did not.

**Fix** (`app.py`): `_served_identity_digest()` (sha1 of the sorted identity map) is now part
of the key in **both** branches. A rename, a conference change, or any identity drift
invalidates the payload. The digest is failure-tolerant — a cache key must never break serving.

**Tests:** `test_a_served_team_rename_invalidates_the_cached_payload`,
`test_a_rename_makes_the_endpoint_recompute` (asserts the response body changes **and** that
serving ran again). Both RED before the fix.

## C4 — publication integrity was insufficient (two findings)

**C4a, offsetting pair:** QA's probe shifted two teams by `+5` and `−5`; every per-key count
and sum stayed identical, so the wrong values were published and served. My manifest was a
per-key `(count, sum)` — order- and offset-blind by construction.

**C4b, extra key:** a row at the same stamp that the pull did not write was not rejected,
because the check only iterated over the keys the pull *intended*.

**Fix** (`d1_write_path.py`): the check is now the **exact row set**. `_read_back_rows()` pages
every `(subject_id, stat_key, value)` readable at the stamp, and publication is refused on any
missing row, any **extra** row, or any **wrong value**. The per-key aggregate helper is deleted
so the blind check cannot be reintroduced by accident.

**Tests:** `test_an_offsetting_pair_of_value_changes_is_caught` (tampers the database, then
asserts on the same connection that counts and sums are unchanged — proving the old check was
blind), `test_an_extra_row_at_the_stamp_blocks_publication`,
`test_publication_is_refused_when_the_readback_does_not_match` (rewritten against the new
read-back). All three RED against `d64bf2a`'s writer — verified by stashing it.

## Round 2 results

| gate | result |
|---|---|
| `python -m pytest tests/` | **156 passed, 4 skipped** (was 151) |
| `node --test tests/js/*.test.mjs` | **21 passed** (was 19) |
| `scripts/run_enforcement_tests.py --all` | **ENFORCEMENT GATE: PASS** |
| `tests/test_d1_provider_specific.py` (scratch D1) | **4 passed** |
| `node scripts/verify_pages_live.mjs` | **ALL LIVE PAGE CHECKS PASSED** |

Also confirmed against `d64bf2a` by stash-and-rerun, so these are RED→GREEN rather than
assertions written to match the new behaviour: C1 (1 failure), C3 (2), C4 (3).

**Unchanged:** no production write, no deployment, no migration. Production still reports
build v62. The four modified `data/` files still match the hashes recorded in §2.

## What I got wrong, plainly

Three of the four counterexamples are in code I wrote in the previous round (C1, C3, C4) and
one is in the page's failure handling (C2). In each case my tests covered the happy path or the
shape I had in mind rather than the counterexample: no test served a publication *without* a
live identity map, none compared a cached payload against a changed input, and my manifest was
the first thing I thought of rather than the strongest thing available. The suite passing at
151 was not evidence for those paths, and QA's probes were.

---

# Round 3 — QA's reader-integrity finding

QA reran `3e14fa2` and confirmed the four round-two counterexamples are fixed, then refuted the
candidate on one remaining requirement:

> the reader trusts a marker after the stored values change … the new exact-row comparison
> protects publication time; the serving reader still checks only row and team counts
> (d1_write_path.py:553–559).

**Correct, and it was the sharper version of my own fix.** I verified values when publishing
and then let the reader trust the marker forever. Drift after publication is exactly what a
marker cannot see.

## Fix

- **Writer:** `_value_digest()` — a sha1 over the exact `(subject_id, stat_key, value)` set that
  was verified — is stored in the marker. Both sides canonicalise the value identically
  (`%.6f`) so an int on one path and a float on the other cannot produce a false mismatch.
- **Reader:** `load_team_analytics()` selects the stamp's rows, recomputes the digest over
  exactly those rows, and **returns `[]` on mismatch** — so the altered snapshot is never
  served; the caller serves its documented fallback and reports degraded. The count check stays
  as a cheap first gate.
- **Legacy markers** (written before digests, including the production marker from
  2026-09-30) carry no digest. They still serve — a missing digest is not a data defect — but
  the serve is reported as `d1 (unverified marker)`, degraded, so it is never *called*
  verified. The next authoritative pull stores a digest and the state becomes `d1`.

## The regression QA asked for

`tests/test_published_snapshot_integrity.py`, 4 tests:

- `test_a_published_snapshot_is_served_when_untouched` — positive control, so the check is not
  simply refusing everything.
- `test_a_value_changed_after_publication_is_not_served` — publish 24,000 rows, then change two
  values under the same stamp from 10/10 to **15/5**; the test first asserts on the same
  connection that the row count, team count and every per-key sum are unchanged (proving the
  old check was blind), then asserts the snapshot is not served.
- `test_the_serve_path_reports_the_failure_and_falls_back` — end to end: the altered value
  never reaches the response, and the serve reports degraded.
- `test_a_marker_without_a_digest_serves_but_is_never_called_verified`.

## Two defects I introduced and caught while doing this

1. **`hashlib` was not imported in `d1_write_path.py`.** The new digest call raised
   `NameError`, and `@_guard` swallowed it into a *silent* "no publication" — the fixture's
   `assert pub is not None` is what caught it. This is precisely the failure mode the skill
   warns about: a guarded write failure is indistinguishable from a quiet one.
2. **My round-two cache key included `_ANALYTICS_SERVE`, which serving itself sets.** The first
   request keyed on `unknown`, its own warm follow-up keyed on `disk (no verified publication)`
   — so every cache hit missed and the payload was recomputed on every request. The existing
   warm-cache test caught it. Both the key and the store decision are now **input-based**: the
   key carries which door produced the payload (marker vs disk input + identity digest) and the
   store is skipped only for an empty payload.

## Correction to the round-one report

Round one claimed the cache "caches neither an empty nor a degraded payload". The accurate
statement is: **an empty payload is never cached, and the door that produced a payload is part
of its key**, so a fallback payload can never satisfy a request that has a verified publication.
Gating the store on the degraded flag was the fragile version of that idea and has been removed.

## Round 3 results

| gate | result |
|---|---|
| `python -m pytest tests/` | **160 passed, 4 skipped** (was 156) |
| `node --test tests/js/*.test.mjs` | **21 passed** |
| `scripts/run_enforcement_tests.py --all` | **ENFORCEMENT GATE: PASS** |
| `tests/test_d1_provider_specific.py` (scratch D1) | **4 passed** |
| `node scripts/verify_pages_live.mjs` | **ALL LIVE PAGE CHECKS PASSED** |

## One consequence to be explicit about

Production's existing marker (`analytics_publication:2026`, written 2026-09-30) predates the
digest, so until the next authoritative weekly pull rewrites it, prod serves are reported as
`d1 (unverified marker)` and `serve.degraded` is **true**. Nothing is blanked and nothing is
hidden — the data still serves exactly as before, it is just labelled for what it is. No
production write was made to change this. If Jeff wants it verified immediately rather than at
the next pull, that is a one-off marker rewrite: a production write, which needs his explicit
approval.