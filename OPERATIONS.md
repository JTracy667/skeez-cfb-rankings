# OPERATIONS — skeezcfb-rankings.com

**READ THIS FIRST.** It is the operations map for the live site: how it runs, how it
deploys, where its data actually lives, and how to prove each claim. The point is that
a fresh session can act without re-deriving any of this from source.

**Companion doc: `docs/DATA_FLOW.md`** — the per-dataset producer → store → reader map
(and the list of traps that have already caused incidents). Read **both** before
touching the site; a change to a producer or reader must update `DATA_FLOW.md` in the
same commit.

**MAINTENANCE RULE (Jeff, 2026-09-27): update this file after EVERY deploy** — at
minimum the `CURRENT STATE` block. If you had to read source to answer "how does X
work", write the answer here before you finish. A stale ops doc is worse than none,
because it is trusted.

Repo: `C:\Users\jtracy\dev\cfb-power-rankings` (see `CANONICAL.md` — this is the only
working clone).

---

## CURRENT STATE

| | |
|---|---|
| Live URL | `https://skeezcfb-rankings.com` (apex is real; `www` CNAMEs to it) |
| Live build | **v61** — `/api/health` → `build`, proven by `code.marker` |
| Code marker | `v61-analytics-identity-d1` |
| Image tag in `wrangler.jsonc` | `cfb-power-rankings:v61` |
| Rollback tag | **v60** — `scripts/cfb_deploy.sh --rollback v60` (v59…v50 also in the registry) |
| Last verified | 2026-09-28 12:42 PT (CTO) — `DEPLOY VERIFIED LIVE: v56`; marker match (not `build`); app image v56 (version 52); all public pages 200 |
| Injuries | D1 `app_state.active_injuries` is the served source (45 teams / 56 tracked at migration). `/api/injuries` and the win-totals build read it. Kill switch `INJURIES_FROM_D1=0`. **D1 `injury_snapshots` is a settled-outcome tracking table, NOT the current injury state.** |
| Quota ledger | `/api/health` `budget` is read from D1 `api_usage` — the ledger of record. The disk mirror `data/budget_ledger.json` is a **local-dev fallback only** (Phase 6). Kill switch `BUDGET_FROM_D1=0`. |
| Archive health | `/api/health` → **`archive`** — `count > 0` means D1 writes are silently NOT landing (F5; before v54 this state was invisible) |
| Container app image | must read `...:v56` via the Containers API — **`build` in `/api/health` does NOT prove this** (see the v52→v53 note) |
| Deploy verifier | `python scripts/verify_container_swap.py --tag vN --marker <CODE_MARKER>` — API-driven, touches the site **once** |
| Enforcement gate | `python scripts/run_enforcement_tests.py` — **blocking** before any deploy (needs `CF_D1_TOKEN`; refuses to run without it) |
| Known-stale docs | `docs/SESSION_HANDOFF.md` (state as of Sep 23 — do NOT trust its state), `CLOUDFLARE_DEPLOY.md` (says `sleepAfter 20m`) |
| Data map | **`docs/DATA_FLOW.md`** — read it before touching any producer or reader |

Verify in one line:
```bash
curl -s https://skeezcfb-rankings.com/api/health   # status ok + build + code.marker + budgets
```

---

## RECENT CHANGES & OPEN WORK

### v60 → v61 (2026-09-28, CTO) — Phase 6 step 6: analytics IDENTITY is durable

`stat_observations` holds numerics only, so the served records' string fields (conf, streak,
mascot, emoji) still came from `data/cfbd_analytics.json` — the image-copied file. v51 fixed the
**numerics**; the **strings** were left behind, and a recycle reverted them to build-time values.
`conf` and `streak` are not static, so that was a real (quieter) staleness hole.

Now `app_state` key `analytics_identity` holds every string field per team, written by
`_store_analytics_identity()` on the same live pull as the numerics. `_served_analytics()` reads
identity from D1; the file is read only when D1 has no identity map at all.

**Two call sites were still on the raw file — one of them a BUILD path:** `get_rankings()` (a
board built from the image file bakes the staleness into D1 — the documented trap) and
`/api/projections`. Both are D1-first now.

**Regression found and fixed:** `tests/test_board_rebuild.py` went red, because its fixture
stubbed the raw file accessor and my change moved `get_rankings()` onto the accessor, so the test
read REAL D1 instead of its frozen two — breaking a test that documents itself as hermetic. The
fixture now stubs `_served_analytics`. That test caught a genuine hermeticity leak, not just a
changed call.

**Disk tier is DONE.** No runtime-written data file is read on the serve/build path any more.
What remains is static only: `cfbd_logos.json`, `teams.json`, `fbs_teams.json` — baked into the
image, never written at runtime (`teams.json` is read by `load_local()` and nothing writes it),
so there is no revert risk.

Suite 107 passed, 0 xfailed. Gate PASS, 0 xfailed.

### v59 → v60 (2026-09-28, CTO) — F3 CLOSED: `teams` has a live writer; gate is fully green

**The last `xfail` is gone.** Enforcement gate **23 passed, 0 xfailed**; suite **104 passed,
0 xfailed** — the first fully-green run of the enforcement gate.

**What F3 really was.** The audit said "D1 `teams` 684 vs served 685". Chasing the 5
mismatches found only ONE real drift:
- `Albany`/`UAlbany`, `UTRGV`/`UT Rio Grande Valley`, `Southeastern Louisiana`/`SE Louisiana`
  are **not** drift — `cfbd_shared.team_aliases()` documents those three cases verbatim and
  the system has always resolved them. A raw `name` comparison cries wolf forever.
- **Anna Maria College (MSCAC) and Defiance College (Heartland)** — both D-III, both in D1
  `teams`, both absent from the analytics universe, both with **zero analytics rows**. That
  pair was the entire finding.

**Fixed (approved live writer):** `d1_write_path.snapshot_team_identity()` + `app._store_team_identity()`
called from `_store_team_analytics`, so the identity list rides **every live pull** instead of a
manual backfill. Writes **canonical** names from `cfbd_shared.teams_by_name()`; writing an alias
over a canonical name would rename a team to something the rest of the system does not recognise.
`_served_analytics()` now also serves D1 `teams` identity rows, **identity only** — no numeric
field is invented, so nothing downstream can read a fabricated value.

**The test was rewritten, not merely un-xfailed.** It now asserts BOTH directions: every team D1
knows is servable, AND every served team resolves to a D1 team (alias-aware). The second is the
dangerous one (a served team D1 cannot identify can never have metrics archived) and it is NOT
satisfied by building the served list from D1 — so the pair is a control, not a tautology.
Counterfactual proven: neutering the identity seed makes it FAIL.

Deploy: ~1.5 min, first `wrangler deploy` applied the image.

### v58 → v59 (2026-09-28, CTO) — Phase 6 step 5: the best-bets tracker is durable in D1

`data/best_bets.json` is **not a cache** — it is the tracked record of which plays were locked
and how they graded, and BOTH `_lock_best_bets` and `_ingest_best_bets` read-modify-write it.
It was on the ephemeral disk, so a recycle silently reset the tracker: locked picks and graded
results were being discarded. Now D1-first (`app_state` key `best_bets`, kill switch
`BEST_BETS_FROM_D1`), write-through mirror, seeded with 32 picks / 20 graded results.

**Deliberate asymmetry:** writes ALWAYS mirror to D1; the kill switch governs READS only, so
flipping it for a rollback can never lose a lock or a graded result.

**The runtime-written disk tier is now CLEARED.** What still reads disk, and why:
- `cfbd_logos.json` — static asset baked into the image, never written at runtime (accepted).
- `teams.json` — **F3**, the last `xfail`, awaiting the backfill-only vs live-writer decision.
- `cfbd_analytics.json` — D1-served since v51; still reads identity fields, finishing next.

Deploy: ~1.3 min, first `wrangler deploy` applied the image, retry not needed.

### v57 → v58 (2026-09-28, CTO) — Phase 6 step 4: the line-movement log is durable in D1

`data/line_movements.json` is a rolling **7-day CLV record** of what the line did — the audit
trail behind the movement feed, not a cache. It was on the ephemeral disk and `_append_movement_log`
was a read-modify-write against that FILE, so a recycle loaded a fresh empty file and appended to
nothing. **The record of the moves was being discarded every 5 idle minutes.**

Now D1-first (`app_state` key `line_movements`, kill switch `MOVEMENTS_FROM_D1`); the append
EXTENDS the durable log; write-through mirror. Seeded with 370 movements.

Deploy: ~1 minute, first `wrangler deploy` applied the image, retry not needed.

### v56 → v57 (2026-09-28, CTO) — Phase 6 step 3: the odds cache is durable in D1

`data/odds_cache.json` exists to **avoid re-buying the day's odds from a metered provider**
after a recycle — that is what the constant says. On an ephemeral disk it could never do that:
every recycle re-fetched and spent PropLine quota for data the site already had. So this one
was not merely stale-reading, it was **silently spending calls it was written to save.** Now
D1-first (`app_state` key `odds_cache`, kill switch `ODDS_CACHE_FROM_D1`), seeded with 636 games.

**Three deploy-machinery faults surfaced on this release** (all now fixed in `scripts/`; see
section 2 — read it before deploying):
1. The first `wrangler deploy` again did not apply the container image — the v52→v53 defect,
   recurring. Step 5b's self-heal caught and re-ran it, exactly as designed.
2. The app-config lag is **minutes, not seconds** (~77s to ~6 min observed). A 120s window
   produced a FALSE failure.
3. `rollback()` never checked the app image — it printed "ROLLBACK COMPLETE" on five HTTP 200s,
   while the rollback had **not landed at all**. Windows are minutes now and the rollback
   CONFIRMS the image or says "ROLLBACK NOT CONFIRMED".

The release shipped and the rollback was theatre; prod was on v57 the whole time.

### v55 → v56 (2026-09-28, CTO) — Phase 6 step 2: injuries serve from D1

**Shipped:** `active_injuries.json` converted. `_load_injuries_doc()` (whole doc, D1-first) and
`_save_injuries_doc()` (write-through) now own it; `_load_active_injuries()`, `/api/injuries` and
`POST /api/injuries/override` all route through them. Kill switch `INJURIES_FROM_D1=0`.

**One of these defects was DATA LOSS, not staleness.** `POST /api/injuries/override` was a
read-modify-write on an **ephemeral** file, so a **manual injury override — a human's correction —
silently evaporated on the next recycle.** And the win-totals **build** path read the same file,
so a recycled container could bake a stale injury adjustment into a board.

**`injury_snapshots` is NOT the home, and looking like it is would have been the trap:** 402 rows,
obvious name, but it is a **settled-outcome tracking table** (`predicted_margin` / `actual_margin`
/ `residual_*`). The door uses `app_state.active_injuries`, the same shape as the Phase 3 fix.

**Cross-file dependency handled:** `/api/injuries/sync` delegates to `scripts/fetch_injuries.py`,
so that scraper now reads its existing store **D1-first** (merging from the file alone could DROP a
D1-only manual override) and writes the durable copy too. Without that the sync would have become a
**silent no-op** — writing the file while the site kept serving the previous D1 snapshot.

**Receipts:** `DEPLOY VERIFIED LIVE: v56`, marker `v56-injuries-in-d1`, app image v56 (version 52),
all pages 200. Guard allowlist **shrank by two**. Suite 95 passed / 1 xfailed; gate 14 passed / 1
xfailed (F3 alone).

### v54 → v55 (2026-09-28, CTO) — Phase 6 step 1: the quota ledger reads D1


**Shipped:** the first file of Phase 6's disk tier. `budget.state()` now reads D1 `api_usage` —
the **ledger of record** — FIRST, with `data/budget_ledger.json` demoted to a local-dev fallback.
Kill switch `BUDGET_FROM_D1=0`.

**Why it was wrong:** `state()` read the disk file first and merged D1 on top with `max()`, which
made an **ephemeral disk file a reconcile input on the serving path** — `/api/health` and
`compute_win_totals()` both read it. Same stale-read class as the analytics bug.

**Why D1-first is correct here, not merely tidier:** `flush()` writes the file and D1 in the
**same call**, so the file can never be ahead of D1; if a flush died between the two, the next
successful flush rewrites the **cumulative** counters anyway. And on the container the file is
wiped with the instance, so it cannot recover anything either.

**Also removed:** `_merge_into`, which existed only to implement the old file-primary max-merge.
A comment marks the spot with the reasoning, so a future session does not "restore" the pattern.

**Receipts:** `DEPLOY VERIFIED LIVE: v55`, marker `v55-budget-ledger-d1`, app image v55 (version
51), all pages 200. Guard allowlist **shrank by two** — `budget_ledger.json` is no longer read in
the SERVE or BUILD scope. Gate 11 passed / 1 xfailed (F3 alone); suite 92 passed / 1 xfailed.
The per-file classification for the remaining 7 is now in `docs/DATA_FLOW.md`.

### v53 → v54 (2026-09-28, CTO) — Phase 5 (F5): a failed archive is visible now

**Shipped:** Phase 5 of `docs/DATA_PERSISTENCE_PLAN.md` (F5 alone — F6 was pulled forward into
Phase 2). `@_guard` in `d1_write_path.py` no longer swallows archive failures: every failure is
recorded to D1 `freshness_events` (`event=archive_failure`) and surfaced in `/api/health` as
`archive` (`count` / `last_fn` / `last_error` / `last_ts`). **The site is still never affected —
the guard still returns 0; this is visibility only.**

**Why it mattered:** a dead archive was indistinguishable from a quiet one. `@_guard` caught the
exception, printed to a log the container discards, and returned `0`, so nothing downstream
could tell "wrote 0 rows" from "never wrote anything" — the same failure shape as the original
v49 bug, one layer down, with every probe still green.

**Deploy was ~2 minutes** (12:21:40 → 12:23). The step-5b app-image gate caught the transition
cleanly: `app image v53 (version 49)` → `app image v54 (version 50)` → `APP IMAGE OK`. No retry
needed. Receipts: `DEPLOY VERIFIED LIVE: v54`, marker `v54-archive-failures-loud`, all pages
200, `archive` present and `count: 0`.

**Reading `archive` in health:** `count > 0` means D1 writes are silently NOT landing. `count:
0` on a freshly booted container is expected (the counter is per-boot; the durable history is
the `freshness_events` rows).

### v52 → v53 (2026-09-28, CTO) — Phase 3 (F2) + the deploy path fixed for real

**Shipped:** Phase 3 of `docs/DATA_PERSISTENCE_PLAN.md`. `load_schedule()` is D1-first (D1
`app_state` key `week_schedule`); the admin `POST /api/schedule/update` override used to be a
read-modify-write on `data/week_schedule.json` on the **ephemeral** container disk, so a
manual override silently evaporated on the next recycle. Verified live: `VERIFIED LIVE: v53 /
v53-schedule-in-d1`, all 8 public endpoints 200.

**This deploy took ~90 minutes and the delay was a real defect, not slowness.** Three distinct
traps, in the order they bit:

1. **`wrangler deploy` reported SUCCESS without applying the container image.**
   `wrangler.jsonc` said `v53` and the Worker's env moved (so `/api/health` reported
   `build: v53`), but the **container APPLICATION** still had
   `configuration.image: ...:v52` (`version: 48`) for a full hour. The app config is what
   decides which image runs. A second `wrangler deploy` applied it (`version: 49`,
   `SUCCESS Modified application`). **`build` is the Worker's tag and it lies.** The gate is
   now: read the app config image from the Containers API and require it to equal the target
   tag before verifying anything else.

2. **The verify loop competed with the recycle it was waiting for.** It curled
   `/api/health` (twice per iteration) every 6m against a 5m `sleepAfter`, so it reset the
   idle timer and the instance never retired — burning its whole budget while never seeing
   new code. **Anything else touching the site inside that window makes it worse**, including
   a person or an agent checking "is it live yet". Fixed: the verifier watches the
   **Containers API** for the instance to retire and then makes exactly **ONE** site request.

3. **The app config is EVENTUALLY CONSISTENT.** A read immediately after the deploy can
   still show the old image; it flipped between two reads ~1 minute apart. Poll it; do not
   judge on one read.

**Do not** verify with a site-polling loop, and **do not** trust `build`. Use:
```bash
python scripts/verify_container_swap.py --tag v53 --marker v53-schedule-in-d1
# phase 0: container app image == v53   (API)
# phase 1: instance retired, ONE request, code marker == v53-schedule-in-d1
```
`scripts/cfb_deploy.sh` now calls this instead of its old curl loop.

### v51 → v52 (2026-09-28, CTO) — results + record durable in D1 (finding F1)

**What was wrong:** results were graded into `data/finals_cache.json` and the SU/ATS record
into `data/record.json` — both **runtime writes on the container's ephemeral disk**
(`sleepAfter` 5m). D1 `games` had nothing newer than **week 3** while the site served
week 5, so the store the parity suite asserts against silently fell behind the site.

**What v52 does:**
- `_fetch_final_scores()` order is now memory → live CFBD fetch **which archives to D1
  `games`** → D1 fallback. The disk cache is deleted from the code path.
- The record lives in D1 `app_state` (`su_ats_record`); `data/record.json` is a local-dev
  fallback. Migrated 889 picks / 332 results.
- **F6 fixed ahead of schedule** (the plan had it in Phase 5): `read_enabled()` (default ON)
  and `write_enabled()` are now separate. Before, reads were gated on the WRITE flag, so
  verifying that serving read D1 required enabling writes to PRODUCTION D1.
- One door per dataset in the data layer: `d1_write_path.snapshot_games()` /
  `load_games()`. `scripts/refresh_d1_games.py` refills a season in one CFBD call.

**Trap recorded:** backfilling D1 made the F1 `xfail` XPASS at once; lifting the marker
there would have left a green test guarding nothing, because the live path still did not
persist. Lifted only once `test_live_finals_fetch_archives_to_d1` proved the code path.
**Rule: never lift an `xfail` because the data got fixed.**

**Left deliberately unfinished:** `best_bets.json` is still read from disk on the serve
path. Phase 2 fixed the results source, not the best-bets BOARD, which needs its own D1
door (Phase 6). Recorded in the guard allowlist rather than claimed.

### v49 → v51 (2026-09-27, CTO) — the D1 serving fix

**What was broken (and for how long).** Two independent defects made the weekly data
pipeline effectively invisible:

1. **The serving path never read D1.** The pull archived fresh metrics to D1 every time,
   and a D1 read function existed (`d1_write_path.load_team_analytics`) — but **nothing
   called it.** `/api/analytics`, `_build_team_map()` and `/api/matchup` all read
   `data/cfbd_analytics.json`, the file baked into the image. Because the container disk
   is ephemeral, the site reverted to the build-time copy on every recycle. Evidence:
   the served payload matched the **2026-09-23** file exactly while D1 held the fresh
   values (Georgia `sp_plus` 28.2 served vs **30.2** in D1), and the board rebuild ran on
   those stale inputs — silently mis-ordering the top of the rankings.
2. **v50** (same night) regenerated the baked dataset and shipped it, which fixed the
   *values* but was only a per-week patch.

**What v51 does.** `_served_analytics()` — **D1 first** — wired into all three serving
call sites. `stat_observations` holds numerics only, so the disk file supplies
identity/string fields (mascot/conf/emoji) and acts as the fallback; the numerics are
overlaid from D1 per team. Kill switch: `ANALYTICS_FROM_D1=0` restores disk-only.

**How it was verified.** With the disk file *deliberately reverted* to the stale Sep-23
copy: D1-first → Georgia `sp_plus` **30.2**; disk-only → **28.2**; 685 teams, 682 sourced
from D1, identity intact. Then live: `build v51` / `marker v51-serve-from-d1`, all five
pages 200, `input_vintages` all 2026, Georgia rank 1.

### Phase 1 of the data-layer plan — DONE (2026-09-28, CTO)

Two enforcement suites now guard the whole defect class, and both were proven to fail when
they should (counterfactual: a disk read injected into `/ping` was caught and named):
`tests/test_served_equals_d1.py`, `tests/test_no_disk_reads_in_serving.py`. Gate:
`python scripts/run_enforcement_tests.py` (refuses to run without `CF_D1_TOKEN`; ~20s).
Full plan, measured `data/` read inventory and per-phase exit criteria:
`docs/DATA_PERSISTENCE_PLAN.md`. Known-broken checks are `xfail(strict=True)`, so fixing
one turns the suite RED until its marker is removed — the worklist cannot rot.

### Open work (in priority order)

> **ACTIVE PLAN: `docs/DATA_PERSISTENCE_PLAN.md`** — phases 0–7, acceptance criteria,
> sequencing and rollback for closing the audit's F1–F8. Superseded by the full audit: `docs/DATA_SYMMETRY_AUDIT.md` (2026-09-28).** It walks
> write/read symmetry across all 17 D1 tables AND the disk tier, with findings ranked
> F1–F8 and receipts. Read that first; the list below is the older, narrower version.

1. **Parity contract test — served payload == D1 latest.** This is the test that would
   have caught the v49/v50 bug. Nothing currently asserts it. **Do this first.**
2. **`stat_observations` is upsert-on-key** → the four weekly anchors collapse to one
   row per metric; intermediate pulls leave no trace, so archives are unauditable.
   Fix = append a `pull_id`/`recorded_at` to the key (row growth ~2×/day) — **Jeff's call**.
3. **`@_guard` swallows archive failures** (`d1_write_path.py`) — a dead archive looks
   healthy. Log failures to `freshness_events` and alert.
4. **Raw API payloads are not retained** (`raw_payloads` = 6 rows) — the source data
   behind the metrics can't be recomputed. Archive the pull's raw JSON.
5. **`players` table is empty** — decide whether player-level persistence is needed.
6. **Reads are gated on `D1_WRITE_ENABLED`** — split into a separate read flag.
7. **Arm the release watcher** (`CFB_ETAG_TRIGGER_LIVE=1`) once the dark run is trusted.
8. **`/api/analytics/fetch` does not stamp the last-pull time** — watcher-triggered
   pulls are invisible to `pull-status` and to the freshness guard.

---

## 1. WHAT IT IS / WHERE IT RUNS

- **FastAPI app** (`app.py`), served by `uvicorn` on **port 8003**, packaged as a
  **Cloudflare Container**, fronted by a thin **Worker** (`src/index.js`) that routes
  all traffic to it. Pages and API are same-origin (no CORS needed).
- **Render is DECOMMISSIONED.** It is not the live path and not the rollback path.
- **GitHub is version control only** — the deployed image is built from the LOCAL
  working tree, not from git.
- Account `90c2c31beec12cb7de1c249ade1eb773` · zone `e45545639cf8693b07d15e5fbd21e873`.
- Container app id `a039e361-4419-451f-ad0a-56464dcc0f65` · `max_instances: 1` ·
  `instance_type: basic` (0.25 vCPU / 1GiB) · **`sleepAfter = "5m"`** (`src/index.js`).
  One warm instance serves all traffic; caches stay coherent. It sleeps after 5m idle
  and cold-starts (~2s) on the next request.
- **Public pages:** `/` · `/analytics` · `/schedule` · `/win-totals` — extensionless
  routes; the `.html` URLs 404 **by design**.
- **Page manifest = the Dockerfile `COPY` line.** The image is built from that
  Dockerfile, so `COPY index.html analytics.html schedule.html win_totals.html ./` is
  the list of pages that exist in prod. **A new page not added there 500s in prod** —
  this is how a page went missing before. `COPY *.py` and `COPY data/ ./data/` are the
  other two lines that matter.

## 2. DEPLOY

Use the script; it encodes the whole procedure and auto-rolls-back:

```bash
# credentials first (never commit them)
export CLOUDFLARE_API_TOKEN="$(python "$LOCALAPPDATA/hermes/profiles/cto/scripts/cf_deploy_token.py")"
export CLOUDFLARE_ACCOUNT_ID=90c2c31beec12cb7de1c249ade1eb773

python scripts/run_enforcement_tests.py        # BLOCKING: served==D1 + no new data/ reads
bash scripts/cfb_deploy.sh vNN <code-marker>   # build -> preflight -> deploy -> verify
bash scripts/cfb_deploy.sh --rollback vNN      # re-point prod at a known-good tag
```

What it does, in order: bump the tag in `wrangler.jsonc` → `wrangler containers build
. --tag ...:vNN --push` → **preflight boot gate** (boots the exact image locally; this
is the gate that catches a missing Dockerfile `COPY`) → set `BUILD_TAG` Worker secret →
`wrangler deploy` → poll `/api/health` until live → 200-check every public page.

**Three things that will bite you:**

1. **`wrangler deploy` is not live.** A warm instance keeps serving the PREVIOUS image
   until it idles out (`sleepAfter` 5m). The verify loop polls every **360s** — the
   interval MUST exceed `sleepAfter` or the probe itself keeps the old instance awake
   and you get a false "never came live" rollback.
2. **`build` alone is not proof.** It is an env var the Worker injects into whichever
   instance answers, so a warm instance on the OLD image reports the NEW tag. Pass the
   release's **code marker** (`app.CODE_MARKER`, surfaced as `/api/health` `code.marker`)
   so verification checks the running CODE.
3. **Don't poke the site during the verify window** for unrelated reasons — traffic
   resets the idle timer and delays the recycle.

**Preflight before any deploy:** `python scripts/selftest_book_of_record.py` and
`python -c "import py_compile; py_compile.compile('app.py', doraise=True)"`.

## 3. DATA PATH — the part that is easy to get wrong

- **The served analytics dataset is `data/cfbd_analytics.json`:** a file COMMITTED to
  git and `COPY`'d into every image (`COPY data/ ./data/`), **overwritten in place** by
  a live pull.
- **Consequence:** a live pull writes to the running container's **ephemeral disk**. A
  deploy or a container recycle **reverts the analytics to the baked build-time copy**.
  So a *durable* dataset change = **regenerate the file in the repo and ship a new
  image** — a live `/api/analytics/fetch` alone is not durable.
- **THE BIG ONE — the serving path ignored D1 (verified 2026-09-27).** The weekly pull
  DOES run and DOES archive fresh data to D1 (`stat_observations`), but
  `_load_cfbd_analytics_file()` / `_build_team_map()` read only the **disk file**, never
  D1. So the pull's result is invisible to visitors: the site serves whatever was baked
  at the last image build. Evidence: `data/cfbd_analytics.json` was last committed
  **2026-09-23**, so the site served Sep-23 analytics until v50 — while D1 held the
  fresh values (Georgia `sp_plus` 30.2 in D1 vs 28.2 served). Rank-affecting: on current
  SP+ Georgia leads Ohio State; the stale board showed the reverse.
  **This is a Cloudflare-cutover regression** (on Render the disk persisted, so pulls
  stuck). `CLOUDFLARE_DEPLOY.md` had noted runtime writes are ephemeral and "re-fetched
  on restart" — but that re-fetch is gated by a 12h age check whose stamp only advances
  on an *anchor-flagged* pull, so after a recycle the site commonly serves the build copy.
  **STRUCTURAL FIX NEEDED: make the serving path read the D1 copy** (Jeff's direction:
  D1 is the system of record). Regenerating the baked file + redeploying is only a
  per-week patch.
- **D1 (`cfb-history`) is the durable archive** for games, ratings, weather_snapshots,
  stat_observations, model_predictions, api_usage, etc. See `D1_SCHEMA_SPEC.md`.
- There is no user/session database. Site state = files in the image + D1.

## 4. REFRESH CADENCE

- **CFBD analytics anchors: Sun / Mon / Tue / Wed at 21:00 America/Los_Angeles** —
  CFBD publishes the composite inputs (SP+ / Elo / FPI / talent) at an unpredictable
  time between Sunday night and Wednesday.
- **A full analytics pull is ~16 CFBD calls** (`drives` and `roster` are one call each
  for the whole league — never make them per-team). The hourly in-window ETag probe
  (`cto-cfb-ratings-etag-probe`) costs ~2.3× the pulls; see
  `docs/CFBD_API_MAP.md` §3d for the measured budget table and the publication-
  atomicity data.
- **Known design gap (Jeff, 2026-09-27):** the anchors refresh only 4×/week, so the
  site can serve up to a day of stale data after a release, even though we already pay
  for a probe that *detects* the release. **The probe is now a release WATCHER**
  (`scripts/probe_ratings_etag.py`): it conditional-GETs **8 composite-relevant
  endpoints** each in-window hour — `ratings/{sp,elo,fpi,srs}`, `stats/season/advanced`
  and `ppa/teams` (the source of the **efficiency** input, ~31% of the weight), plus
  `talent` and `player/returning` — settle-gates each independently, and when armed
  triggers the pull immediately. **Watching only the ratings trio would MISS an
  efficiency update.** **DARK by default** (log-only); armed with
  `CFB_ETAG_TRIGGER_LIVE=1`. Cost scales with the watch set: **~9.0% of the CFBD
  allowance** (8 calls × 78 in-window hrs/wk), up from 2.3% ratings-only. It reuses the
  existing hourly cron (`cto-cfb-ratings-etag-probe`) — no new job. The trigger POSTs
  the admin-gated `/api/analytics/fetch`: **the same pull path, so D1 archiving is
  unchanged**. **Caveat:** `/api/analytics/fetch` does NOT stamp the last-pull time
  (only `refresh-if-due` does), so a watcher-triggered pull is invisible to
  `/api/analytics/pull-status` and to the freshness guard. The anchors stay the backstop.
- Wired via `wrangler.jsonc` crons `0 4 * * 1,2,3,4` **and** `0 5 * * 1,2,3,4`
  (21:00 PT = 04:00Z under PDT / 05:00Z under PST — both hours or the anchor is missed
  half the year; cron days are UTC, so Sun 21:00 PT is Mon 04:00Z). The Worker's
  `scheduled()` POSTs **`/api/analytics/refresh-if-due`** with `X-Admin-Token`;
  the container self-determines due-ness, so the route is idempotent.
- **Freshness guard** (`FRESHNESS_GUARD=1`, `FRESHNESS_MAX_AGE_HOURS=12`) pulls whenever
  analytics is older than 12h regardless of the anchor — this is what turns a missed
  anchor into "stale until the next visitor" instead of a multi-day gap.
- **Scheduler thread** starts at module import (`_start_scheduler()`, `app.py`), sleeps
  10s then ticks; `REFRESH_INTERVAL_SECONDS` = **3600** in the container (app default 6h).
- **Weather** has its own append-only D1 stream (`weather_snapshots`, hourly,
  pre-kickoff only). **Lines/odds** refresh hourly. **Injuries**, **ESPN/CFBD games**
  as noted in `docs/DATA_TIER_MAP.md`.
- **Locally, disable all of it** so a test/dev run spends no metered quota:
  `REFRESH_INTERVAL_SECONDS=0 CFB_SKIP_BOOTWARM=1` (`0` makes `_start_scheduler` return
  immediately; the boot-warm thread is otherwise started at import).

## 5. OPS ROUTES AND THE ADMIN GATE

- Ops **POST** routes require header **`X-Admin-Token`** matching `ADMIN_TOKEN`; the
  dependency **fails closed (503)** when the token is unset. `ADMIN_TOKEN` lives in
  repo `.env`, in `.admin_token`, and as a Worker secret passed into the container via
  `src/index.js` `envVars`. FastAPI `/docs`, `/redoc`, `/openapi.json` are disabled.
- Gated (8 + these): `rankings/refresh`, `refresh`, `analytics/refresh-if-due`,
  `schedule/update`, `injuries/sync`, `injuries/override`, `record/ingest`,
  `record/repair-ats` — plus **`/api/analytics/fetch`** and the admin-only `/api/matchup`.
- **NOT gated: `/api/schedule/fetch`** (public, throttled via a cached payload — a public
  page cannot hold a secret). `/api/analytics/fetch` **is** gated: the analytics page's
  manual refresh button was removed, so nothing public calls it any more.
  *(Verified against `app.py` decorators 2026-09-27; an earlier version of this file
  wrongly listed both as ungated.)*

## 6. THE MODEL — what is live, and what is reference-only

- **Enabled composite inputs (must sum to 1.0):** `sp_plus` .185567 · `efficiency`
  .309278 · `talent` .319588 · `experience` .185567 (V6.1).
- **Nominated-but-INERT keys (weight 0.00):** `fpi`, `srs`, `elo` (dropped in V6.1,
  kept as named keys so the change is reversible in one line), plus `fcs_rating` and
  `massey` (awaiting Jeff's approval — do not move without it).
- **`srs` is REFERENCE-ONLY: weight 0.00.** It is still fetched and displayed, but it
  contributes nothing to the composite. **Changing what SRS vintage is displayed does
  NOT change the board.** (Several code comments and older docs still say "12% of the
  composite" — that is a V6.0-era claim and is WRONG as of V6.1.)
- **SRS / Elo / talent fall back to the PREVIOUS season** when CFBD has not published
  the current one. The serving vintage is recorded honestly in **`data/rating_vintages.json`**
  and per-row as **`srs_source_year`**, and exposed by **`/api/rankings` `input_vintages`**.
  A consumer should label the vintage, or show nothing — never imply recency.
- Weights need **Jeff's sign-off** for LIVE changes. Backtest-only changes are authorised
  by the work order. Never interleave a deploy with a backtest work order.
- **Rankings page sorts by composite rank ALWAYS**; AP/Coaches are reference-only, never
  sortable. Projected scores are never normalised/clamped. Schedule data comes from CFBD
  `/games` — ESPN's scoreboard truncates to 25 events/week, never wire it back in.

## 7. QUOTAS

Surfaced live by `/api/health` → `budget`.

| Source | Limit | Notes |
|---|---|---|
| CFBD | 30,000 | ratings/stats/publication pulls |
| PropLine | 5,000/day | auto-stop at 95% (serve stale-but-honest); 80% = tier-upgrade decision |
| The Odds API | 20,000 | historical = **10 credits per market** |
| D1 writes | 2,000,000/day guard | account is Workers **paid** (50M rows/mo) |

- **Ledger of record = D1 `api_usage`** (bucket day/month, source, calls, remaining).
  Read it **before and after** any deploy/test — a container boot re-buys
  boot-dependent data, so a test-heavy day multiplies burn with no change in site need.
- **Tests must never hit a live metered fetcher** (use the skip flags in §4).

## 8. TRUTH SOURCES (don't infer, read)

| Question | Endpoint |
|---|---|
| Is prod healthy / what build+code is live? | `GET /api/health` (`build`, `code.marker`, `budget`, `degraded`) |
| Which vintage is each rating served from? | `GET /api/rankings` → `input_vintages` |
| When did the weekly analytics sync last run? | `GET /api/analytics/pull-status` |
| Did the container actually restart? | Cloudflare Containers API: `GET /accounts/<id>/containers/applications/a039e361-4419-451f-ad0a-56464dcc0f65/instances` → `started_at`, `state` (`/api/health` has no uptime field) |
| Counter/anchor wake-ups | `npx wrangler tail` |

## 9. LOCAL RUN + VERIFY

```bash
# run the app locally without spending metered quota
REFRESH_INTERVAL_SECONDS=0 CFB_SKIP_BOOTWARM=1 python -m uvicorn app:app --port 8003
curl -s localhost:8003/api/analytics | head -c 200
```

- **Never trust `node --check`** on page JS: it passes files with undefined-var template
  refs whose runtime `ReferenceError` kills rendering mid-page while a try/catch masks it
  as "server unreachable". Execute page JS in a Node `vm` sandbox against the live API
  and count tbody rows.
- Cloudflare in front of the Worker **blocks header-poor clients** (403 `error code:
  1010`) — scripts must send a real `User-Agent` + `Accept` (curl/browser pass).
- **MSYS/bash note:** use `powershell -NoProfile -Command "Stop-Process -Id N -Force"` to
  kill processes; `taskkill //F //PID` silently no-ops here.

## 10. DOC INDEX — authoritative vs stale

| Doc | Status |
|---|---|
| **`OPERATIONS.md`** (this file) | **AUTHORITATIVE for live state + procedures** |
| `CANONICAL.md` | Repo-location rules |
| `D1_SCHEMA_SPEC.md` | D1 schema — authoritative |
| `docs/DATA_TIER_MAP.md` | What data comes from where + refresh tiers |
| `docs/CFBD_COMPOSITE_INPUTS.md` | CFBD input field definitions |
| `docs/MODEL_WEIGHTS_REVIEW.md`, `docs/BACKTEST_*.md` | Model/backtest record |
| `docs/API_CATALOG.md`, `docs/ODDS_API_MAP.md`, `docs/PROPLINE_API_MAP.md` | API field/pricing catalogs |
| `CLOUDFLARE_DEPLOY.md` | Deploy background — **says `sleepAfter 20m`; live value is 5m** |
| `docs/SESSION_HANDOFF.md` | Snapshot 2026-09-23 — **state is STALE (said v27)**; its deploy-cred grep procedure is SUPERSEDED by `scripts/cf_deploy_token.py` |