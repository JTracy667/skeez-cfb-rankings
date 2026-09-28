# DATA PERSISTENCE PLAN — phases, deliverables, acceptance

**Status: PLAN ONLY. No implementation begun.** Written 2026-09-28 (CTO), for Jeff's
approval. Nothing in this document has been coded, deployed, or scheduled.

**Authority / scope.** This plan exists to close the defect class found by
`docs/DATA_SYMMETRY_AUDIT.md` (findings F1–F8) and to make that class unable to recur —
not merely documented. Read `docs/DATA_FLOW.md` (per-dataset map) and `OPERATIONS.md`
(procedures) alongside it.

**Decisions already locked (Jeff, 2026-09-28):**

| decision | ruling |
|---|---|
| F4 history model | **Option B — append-only.** Keep every pull's values; prune duplicates after the season. |
| `players` table | **KEEP.** Reserved for player-vs-player matchups later. Currently hooked up to nothing; do not delete, do not wire without a work order. |
| F3 `players` action | Out of scope beyond the note above. |

---

## 0. THE GOVERNING PRINCIPLE (why this is not "one more patch")

The v50 incident was not caused by ignorance — `CLOUDFLARE_DEPLOY.md` already warned that
runtime writes sit on ephemeral disk. It happened because **nothing failed**. A written
trap is a note; a failing test is a control. The agent that missed the wiring also wrote
the note, so the note is not evidence of anything.

Therefore this plan's first deliverable is **not a fix — it is the instrument that proves
fixes** (Phase 1), and its final deliverable is a **traps → tests** table in which every
trap is either enforced mechanically or explicitly labelled an accepted limitation
(Phase 7). Any trap that ends up in neither column is a bug in this plan.

---

## PHASE 0 — Read, confirm, baseline

**What it is.** No code. Read `OPERATIONS.md`, `docs/DATA_FLOW.md`,
`docs/DATA_SYMMETRY_AUDIT.md`, `CANONICAL.md` first, then record the system's actual
starting state as command output pasted into this file:

1. Prod state: `/api/health` → `status`, `build`, `code.marker` (expect **v51** /
   `v51-serve-from-d1`). Anything else means prod moved and this plan must be re-based.
2. Repo state: `git log -1`, `git status --short` (expect clean tree).
3. Credential + D1 reachability: `cf_deploy_token.py` resolves; one D1 query returns.
4. **D1 baseline per table**: row count for all 17 tables (the "before" numbers).
5. **Recency baseline**: newest scored game in D1 `games` (the F1 receipt), newest
   `stat_observations` write, `players` = 0.
6. **Parity baseline**: for a handful of known keys, compare served vs D1 (the F2/F1
   "before" state).
7. **Enforcement baseline**: run both audit scripts from the audit doc and save output.

**What it accomplishes.** Turns every later claim into a measured delta instead of an
assertion, and proves the documentation is actually sufficient to work from — if Phase 0
cannot be completed from the docs alone, that is itself a finding to fix before coding.

**Verification.** The pasted output *is* the verification. No receipts, no Phase 1.

**Rollback.** None needed — read-only, no writes, no deploys.

**Done when.** The baseline block at the bottom of this file is filled with real output.

**Risk.** None. This is deliberately cheap.

---

## PHASE 1 — The enforcement instruments (tests before fixes)

**What it is.** Two checked-in test suites. No behaviour change, no deploy required for
them to be useful locally.

- **1a. Parity test — `tests/test_served_equals_d1.py`.**
  For each dataset in the chokepoint set, assert the payload the site *serves* equals the
  newest D1 row for the same key. Starts with: team analytics (`/api/analytics` vs
  `stat_observations`), schedule, rankings boards (`slate_cache`), and results (`games`).
- **1b. Chokepoint guard — `tests/test_no_disk_reads_in_serving.py`.**
  Asserts that no function reachable from a public route opens a file under `data/`.
  A small allowlist is permitted for genuinely static reference files
  (`cfbd_logos.json`, `fbs_teams.json`), and **each allowlist entry must carry a written
  justification in the test file** — an unjustified entry is itself a test failure by review.

**What it accomplishes.** Converts the trap list from prose into failures. **Both suites
are expected to FAIL on day one** — the failure list *is* the worklist for Phases 2–6, and
it is the evidence that the instrument works rather than being decorative.

**Verification (counterfactual, mandatory).** Introduce a deliberate disk read on a scratch
branch and prove 1b fails; point a served key at a stale D1 row and prove 1a fails. A test
that passes on already-correct input proves nothing — that mistake was made twice tonight.

**Rollout / rollback.** Test-only commit. If a suite would block a deploy before its
subject is fixed, it ships marked expected-fail with the reason — it is never deleted.

**Done when.** Both suites exist, cover the datasets above, the expected-fail list is
recorded in this file, and both are runnable by one command.

**Risk.** Low. The main risk is a guard test that is too clever and gets skipped — the
counterfactual check is what prevents that.

---

## PHASE 2 — F1: results persisted to D1 (the frozen `games` table)

**What it is.** Make the live path write final scores into D1 `games`. Today `upsert_games`
is reachable only from `scripts/backfill_d1.py`, so the table is a snapshot of the 9/21
backfill (`0` scored games for 2026 week ≥ 5) while grading writes `data/record.json` on
disk. Then backfill the gap from 9/21 to now.

**What it accomplishes.** Kills the "any D1-based analysis silently excludes everything
since 9/21" hole. `games` becomes trustworthy for recency, which later phases and the
backtest rig depend on.

**Verification.** D1 shows scored games for the current week; the parity test covers
`games`; counterfactual = disable the new write and show the parity test goes red.

**Rollout / rollback.** One deploy, own code marker; rollback = tag revert. Writes are
additive upserts — no destructive step, no schema change.

**Done when.** Newest completed week present in D1 `games`, and parity green for results.

**Risk.** D1 write volume: a container boot re-buys boot-dependent data and the container
cycles often, so the write must respect the write ledger / daily cap (`D1_DAILY_WRITE_CAP`)
rather than firing per request. Watch the ledger before and after.

---

## PHASE 3 — F2: schedule served from one source

**What it is.** Remove the double source. `load_schedule()` reads `data/week_schedule.json`
(called by `api_schedule()`), while `/api/schedule` also reads the durable `slate_cache`.
Introduce one D1-first accessor (`served_schedule()`), the same shape as
`_served_analytics()` from v51: D1 authoritative, disk demoted to fallback.

**What it accomplishes.** One source of truth per page. A container recycle can no longer
revert the schedule — the exact failure that hid for a week on analytics.

**Verification.** Parity test covers the schedule; counterfactual = point the disk copy at
a stale value and prove the endpoint still serves D1's.

**Rollout / rollback.** One deploy, own code marker, tag revert. Kill switch
`SCHEDULE_FROM_D1=0` mirroring `ANALYTICS_FROM_D1=0`.

**Done when.** Exactly one authoritative read path for the schedule; parity green.

**Risk.** Medium — this is the live serving path. Mitigated by the kill switch and by
shipping it as its own deploy rather than bundled.

---

## PHASE 4 — F4: append-only history for `stat_observations` (Jeff: Option B)

**What it is.** Change the storage key so a pull **adds rows instead of replacing them**:
put the pull's timestamp/id into the uniqueness instead of only
`(subject_type, subject_id, season, stat_key, week)`. Reads take the newest row per key.
No history can be backfilled — this starts from the first pull after the change.

**What it accomplishes.** Revision history (what CFBD changed, and when), an audit trail in
which a silently failing archive is visible from the data itself, and honest
"what did we know at the time" backtests.

**Cost, stated plainly.** ~28,000 extra rows per pull, ~2 pulls/day. D1 is on the paid
Workers plan (50M rows written/month), so it is affordable; the cost is a table that grows,
plus a "newest only" filter on every read. Duplicates can be pruned after the season
(Jeff's note).

**Verification.** Two consecutive pulls produce two rows for the same
(team, metric, week) with different pull stamps; reads return the newest; parity still green.

**Rollout / rollback.** **Highest-risk phase.** Recommended sequencing: write under the new
key first while reads still take the newest per key, verify, then remove the overwrite
behaviour. Rollback = code revert; the extra rows are harmless.

**Done when.** Two consecutive pulls are both visible, reads return the newest, parity green.

**Risk.** Schema + read-semantics change on the table that feeds the composite inputs. Do
not run this in the same deploy as Phases 2–3.

---

## PHASE 5 — F5 + F6: make failure loud

**What it is — two small changes to the D1 layer.**

- **F5.** Every `snapshot_*` in `d1_write_path.py` is wrapped in `@_guard`, which catches
  all exceptions, returns `0`, and prints "failed (site unaffected)". Record each failure to
  `freshness_events` (the table already exists and is already read by ops) and surface it in
  the health/status payload, so a dead archive is visible rather than assumed-healthy.
- **F6.** D1 **reads** are currently gated on `D1_WRITE_ENABLED` via `d1_write_path.enabled()`.
  Split them: a separate read flag (default on) so that turning writes off does not silently
  make reads return `[]` and fall back to disk.

**What it accomplishes.** A silently failing archive becomes a visible event; the read path
stops depending on a write toggle. (This coupling is what made my first counterfactual test
report a false FAIL — it will bite a future session the same way.)

**Verification (counterfactual).** Force an archive exception and show the failure is
recorded and visible; set writes off with reads on and show reads still work.

**Rollout / rollback.** One deploy, own marker, tag revert. Additive; no schema change.

**Done when.** A deliberate archive failure produces a visible record, and reads are
independent of the write flag.

**Risk.** Low.

---

## PHASE 6 — The disk tier: one door per dataset (+ F3)

**What it is.** The audit found **11 `data/*.json` files read at serve/build time off the
container's ephemeral disk** (`week_schedule`, `odds_cache`, `line_history`,
`line_movements`, `best_line_store`, `best_line_ts`, `record`, `finals_cache`, `best_bets`,
`active_injuries`, plus the analytics file which v51 already demoted). Some are re-fetched
on boot — which is the same unverified assumption that hid the analytics bug. For each file:

1. Decide and record: **D1-authoritative** (convert, disk becomes fallback) or
   **explicitly transient** (a written justification — e.g. a pure cache that is provably
   rebuilt before it can be served).
2. Convert the D1-authoritative ones to a single accessor, exactly like `_served_analytics()`.
3. Route every read through that accessor — no other code may open the file.

Plus **F3:** `teams` is read by the live path but written by nothing on it (`upsert_teams`
exists only in backfill scripts). Either wire a live write path, or declare `teams`
backfill-only and put its refresh on the weekly cron — and enforce the choice in the guard
test. `players` stays as decided (KEEP, unwired).

**What it accomplishes.** This is the phase that removes the *class*. Once every dataset has
one door and the guard test fails on any direct disk read in serving code, a v50-style defect
cannot ship — regardless of who is writing the code that week.

**Verification.** Guard test green (no unjustified disk reads in serving code); parity green
per converted dataset; counterfactual = add a direct disk read to a scratch branch and show
the guard fails.

**Rollout / rollback.** Each file converts as its own small, independently deployable,
independently revertible change. Kill switch per dataset, mirroring `ANALYTICS_FROM_D1`.

**Done when.** Every file in the list is either D1-authoritative with one accessor, or
carries a written "explicitly transient" justification recorded in `docs/DATA_FLOW.md`.

**Risk.** Medium overall, low per change. The risk here is drift — converting four files and
losing track of the rest — which is why the guard test, not discipline, is the completion
criterion.

---

## PHASE 7 — Docs and enforcement closure

**What it is.** With the code settled:

- `OPERATIONS.md` — `CURRENT STATE` (build, marker, rollback tag, verify date) plus a
  `RECENT CHANGES` entry per phase with receipts.
- `docs/DATA_FLOW.md` — every producer/reader row that changed, in the same commit as the
  change (the standing rule).
- `docs/DATA_SYMMETRY_AUDIT.md` — mark F1–F7 **resolved / open / accepted** with the receipt
  for each. Nothing gets marked resolved without a receipt.
- **The traps → tests table** (below) completed, and added to `CANONICAL.md`'s read-first
  gate so it is seen before any work on this repo.

**What it accomplishes.** Closes the loop Jeff asked for: the docs describe the system as
built, *and* each trap is listed next to the mechanical thing that catches it. It also makes
the honest distinction visible — the traps that are **not** testable are labelled as accepted
limitations instead of being counted as protection.

**Done when.** The table is complete, every audit finding has a disposition with a receipt,
and the docs are committed alongside the code they describe.

---

## SEQUENCING

```
Phase 0 (baseline, read-only)
   └─ Phase 1 (tests)        ← must precede fixes: it is how a fix is proven
        ├─ Phase 2 (F1 results)      ─┐ each its own deploy,
        ├─ Phase 3 (F2 schedule)      │ own code marker,
        ├─ Phase 5 (F5+F6 loud)       │ own rollback
        ├─ Phase 6 (disk tier + F3)  ─┘
        └─ Phase 4 (F4 append-only)  ← LAST: schema risk, do not bundle
             └─ Phase 7 (docs closure)
```

**Why Phase 4 last:** it changes the key of the table that feeds the composite inputs. The
wiring fixes first means the parity test is already green, so if Phase 4 misbehaves the
cause is unambiguous.

---

## NON-GOALS (explicit)

- No changes to model weights, calibration constants, or composite inputs.
- No changes to what external data sources track or how parsers interpret them.
- No new hosting, no new infra, no new spend.
- No `players` implementation (KEEP + note only).
- No `app.py` decomposition. That is a separate quality question and is deliberately not
  bundled here.

---

## SUCCESS CRITERIA FOR THE WHOLE PLAN

1. The guard test fails if serving code opens a `data/` file (proven by counterfactual).
2. The parity test fails when served and D1 disagree (proven by counterfactual).
3. Both run in the deploy verify step, so a regression cannot reach live.
4. No dataset is served from ephemeral disk without a written justification.
5. Every audit finding F1–F8 has a disposition and a receipt.
6. A deliberately broken archive produces a visible failure event.

---

## TRAPS → TESTS (the answer to "how do we not fall in again")

| trap | enforced by |
|---|---|
| serving reads the baked image file instead of D1 | `test_no_disk_reads_in_serving.py` + `test_served_equals_d1.py` |
| served payload drifts from D1 | `test_served_equals_d1.py`, run in the deploy verify step |
| a pull writes but nothing archives | F5 failure events + parity test |
| a weak probe passes a wrong change live | counterfactual check required in every phase |
| `build` tag mistaken for proof of live code | already enforced — `cfb_deploy.sh <tag> <marker>` + `code.marker` |
| a page missing from the Dockerfile `COPY` line 500s in prod | smoke check enumerating the COPY line |
| `node --check` passing broken page JS | Node `vm` execution against the live API (existing rule) |
| not testable (accepted limitation, must be labelled as such) | D1 index-maintenance row counts; CFBD's own revision behaviour |

---

## PHASE 0 BASELINE — COMPLETE 2026-09-28

Recorded from live command output. Phase 0 status: **DONE** (read-only, no writes, no
deploys). Phase 1 may begin from these numbers.

**1. Prod state**
```
status: ok | build: v51 | code.marker: v51-serve-from-d1     ✔ as expected
```

**2. Repo state**
```
head:   1d72ba3 (2026-09-28) docs: DATA PERSISTENCE PLAN (phases 0-7) -- plan only
dirty:  0 files                                              ✔ clean
```

**3. Credentials / D1 reachability**
```
cf_deploy_token.py resolves OK ; D1 query returns        ✔
```

**4. D1 table counts (all 17) — the "before" numbers**
```
api_usage            32      players               0
app_state             3      rankings_daily      175
backtest_runs        12      raw_payloads          6
closing_lines      7,184     served_snapshots    130
freshness_events     202     slate_cache           4
games             21,204     stat_observations 92,406
injury_snapshots     402     teams               684
model_predictions    272     weather_snapshots 43,806
odds_snapshots   189,528
```

**5. Recency baseline**
```
games (season 2026)         : max week 15 scheduled, 1,065 games WITH scores
games 2026 week >= 5 scored : 0          ← F1 receipt, unchanged
newest stat_observations    : 2026-09-28T04:02:41Z (the 21:02 PT anchor pull)
players                     : 0
```

**6. Parity baseline (served vs D1, newest archived week)**
```
PARITY: PASS — 8/8 checked (Georgia, Ohio State, Alabama, Texas × sp_plus, srs)
served teams: 685
```
Note: team analytics is green **because v51 fixed it**. This is therefore NOT a
sufficient parity test — Phase 1's parity suite must cover the datasets that are still
broken (results/`games`, the schedule), or it will pass on day one and prove nothing.

**7. Enforcement baseline (audit scripts)**
```
SERVE PATH
  / /analytics /schedule /win-totals   reads: app_state
  /api/health                          reads: app_state, api_usage
  /api/rankings                        reads: app_state, slate_cache
  /api/schedule                        reads: app_state, slate_cache
  /api/analytics                       reads: stat_observations, app_state, teams
BUILD PATH
  get_rankings(force=True)             reads: stat_observations, api_usage, teams
  load_schedule()                      reads: (none — disk file)
  compute_win_totals(force=True)       reads: (none)
```

### What the baseline establishes

1. **The F1 defect is confirmed on the day we start:** zero scored games for 2026 week ≥ 5.
2. **Team analytics parity is already green** (v51), so the parity suite must be aimed at
   the *broken* datasets or it is decoration.
3. **The disk tier is real and measured:** `load_schedule()` and `compute_win_totals()`
   touch D1 zero times — their data comes from ephemeral disk.
4. **Some streams are demonstrably live** and were not part of the defect:
   `odds_snapshots` (+7,546), `weather_snapshots` (+3,024), `served_snapshots` (+2),
   `freshness_events` (+1) all grew between the audit and this baseline. The frozen ones
   are `games` (results) and the disk files.
5. **`players` = 0**, kept by decision, unwired.

### Baseline limitations — recorded honestly

- The SQL capture cannot see **disk-file reads**; the disk tier is measured from the
  `*_FILE` constants and their call sites (audit §4), not from this capture.
- The parity check covers 8 keys on 4 teams. It is a smoke-level check, not exhaustive.
- `games` "max week 15" is the *scheduled* season extent, not evidence of recent results.
  The meaningful figures are the scored-game counts.


---

# PHASE 1 — COMPLETE (2026-09-28)

**Status: DONE.** Two enforcement suites, both green, both proven to fail when they should.
No behaviour change shipped: this phase adds instruments, not fixes.

## Artifacts

| File | What it is |
|---|---|
| `tests/test_served_equals_d1.py` | Parity: what the site SERVES must equal what D1 STORES. |
| `tests/test_no_disk_reads_in_serving.py` | Chokepoint guard: no `data/` read on the serve, build or import path. |
| `tests/_reads_probe.py` | Measures `data/` reads for one scope, in a fresh interpreter. |
| `scripts/run_enforcement_tests.py` | The gate. Refuses to run without D1 credentials. |

Run it: `python scripts/run_enforcement_tests.py` (~20s; `--all` adds the rest of the suite).

## What each instrument actually enforces

* **Chokepoint guard — "allowlist subset".** Measures every `data/` read on three scopes and
  asserts the set is a subset of a justified allowlist. It therefore passes today while
  naming every known offender, and it fails on ANY new read. Each allowlist entry must name
  the finding that owns it (`F<n>`) or be marked `PERMANENT`, plus the phase that removes
  it. The exit criterion of each later phase is: *delete its entries from the allowlist.*
* **Parity — strict vs known-broken.** Strict checks must always pass. Known-broken checks
  carry `@pytest.mark.xfail(strict=True)`, so when a phase fixes one it starts *passing* and
  pytest reports `XPASS(strict)` **as a failure** — the signal to remove the marker. The
  worklist cannot silently rot into false coverage.
* **The differential test (the important one).** A parity check where both sides agree *by
  construction* proves nothing: the disk file was regenerated FROM D1 in v50, so served and
  stored match whether or not D1 is actually read. `test_serving_follows_d1_even_when_the_disk_file_disagrees`
  poisons the on-disk file with a sentinel and asserts the site serves D1 anyway. **That is
  the v50 defect as an executable assertion.**

## Counterfactuals run (the instruments are proven, not assumed)

| # | Injected | Expected | Result |
|---|---|---|---|
| 1 | `open(data/rating_vintages.json)` added to the served `/ping` route | guard fails, naming the file and route | **FAILED: `rating_vintages.json <- GET /ping`** ✔ |
| 2 | on-disk analytics poisoned while D1 enabled | serving must follow D1 | **differential test passed; served followed D1** ✔ |

Both injections were reverted; `app.py` verified clean afterwards.

## MEASURED `data/` reads — the F1/F2/F3 map, by scope

Measured with `CFB_GUARD_DISCOVER=1`, fresh interpreter per scope:

**SERVE (9)**
```
cfbd_analytics.json  <- /api/analytics, /api/best-bets, /api/projections
odds_cache.json      <- /api/odds
line_movements.json  <- /api/line-movements
active_injuries.json <- /api/injuries
teams.json           <- /api/best-bets, /api/health
budget_ledger.json   <- /api/health
finals_cache.json    <- /api/best-bets/record
record.json          <- /api/record
best_bets.json       <- /api/best-bets, /api/best-bets/record
```
**BUILD (5)** — worse than serve: a board built from a stale file has the staleness
**persisted into D1**.
```
week_schedule.json   <- load_schedule()          <-- F2, located at last
cfbd_analytics.json  <- compute_win_totals()
teams.json           <- compute_win_totals()
budget_ledger.json   <- compute_win_totals()
active_injuries.json <- compute_win_totals()
```
**IMPORT (1)** — `cfbd_logos.json`, static reference, `PERMANENT` (no revert risk).

## Findings this phase produced

* **F2 is located, and it is on the BUILD path:** `load_schedule()` reads
  `data/week_schedule.json`. Serving `/api/schedule` does *not* touch disk — so the earlier
  framing was wrong and the risk is worse, because the board gets persisted.
* **New exposure not in the audit:** `/api/health` reads `budget_ledger.json` off disk while
  **D1 `api_usage` is the ledger of record**. The quota display can therefore be stale in
  exactly the v50 way.
* **F3 is confirmed empirically and is concrete:** D1 `teams` holds **684** teams, the site
  serves **685**, and two D1 teams cannot be served at all — **Anna Maria College**,
  **Defiance College**. Two populations, two sources, no reconciliation.
* **F1 confirmed by test.** `newest scored week in D1 = 3`; the site serves week 5.

## Three things learned the hard way (each is now guarded against)

1. **Patching only `io.open` hides the defect.** A bare `open(...)` resolves via
   `builtins.open`, and that blind spot concealed exactly the
   `open(_CFBD_ANALYTICS_FILE)` call — the v50 file. Both must be patched.
2. **A test whose two sides agree by construction is decoration.** The first parity suite
   passed against a disk file regenerated from D1. Only forcing them apart revealed it.
3. **In-process measurement is order-dependent.** The guard passed alone and failed in a
   combined run; caches warmed by another module changed the answer. It now measures in a
   fresh subprocess. One read (`fbs_teams.json`) sits on a branch gated by an external call
   and is marked `INTERMITTENT`: waived from the stale check only, still a hard failure if it
   appears unlisted.

## Still open from this phase

* Wire the gate into the deploy path (`scripts/cfb_deploy.sh` / Phase 7) — right now it is
  runnable, not yet blocking.
* The gate takes ~20s and needs `CF_D1_TOKEN`; it must run in the container/CI context with
  store credentials or it correctly refuses.