# D1 PHASE 2 — RISK REGISTER & DURABLE PLAN
**Status:** Planning (Jeff directive: find every shortfall BEFORE implementation). Complements D1_SCHEMA_SPEC.md.
**Rule:** nothing in Phase 2 ships until every OVER/TIGHT risk has an owner and a decision.

---

## A. API BUDGET RISKS (the ones that stop data flowing)

### A1. The Odds API — PAID, 20,000 credits/MONTH (Jeff-confirmed 2026-09-21) — **LIKELY OK, verify burn**
- Jeff's actual plan: 20,000 credits/month (the earlier 500-free-tier concern is void).
- Budget math: at 1 credit/market/region/poll, hourly polling (24/day) of up to ~25
  market-region combos fits within 20K/month with margin. The audit's job is to measure
  the app's ACTUAL credits/day and confirm real burn < ~60% of budget (12K/month) so
  game-day bursts and retries can't breach the cap.
- Retain: day-type polling (off-season pause) as the efficiency lever — it's now about
  conservation, not survival.

### A2. CFBD — PAID Tier 2, 30,000 requests/MONTH (Jeff-confirmed; live-verified 2026-09-21) — **GREEN**
- Live-verified: `X-CallLimit-Remaining: 27,139` at 10:00 PT Sep 21 (≈2,861 used this
  cycle), 1 request = 1 credit, endpoints healthy.
- Backfill ~3,000 requests ≈ 10% of the monthly budget — fits in 1–2 days without
  threatening the live refresh. Checkpointing retained as insurance, not necessity.
- Guard: keep a monthly budget counter in the app (live + backfill share the 30K pool).

### A3. PropLine — PAID, 5,000 requests/DAY (Jeff-confirmed 2026-09-21) — **LIKELY OK, verify burn**
- Cap is daily (resets each day), generous vs. an hourly poll cadence. Audit confirms
  per-poll request count (if one poll = 1 request: 24/day = 0.5% of budget; if the app
  fans out per-game, the number changes — measure, don't assume).
- Same 60%-of-budget guardrail as A1: game-day bursts must not breach 3,000/day.

### A4. Enforcement layer (mitigates all of A)
- **Standing policy (Jeff):** tier upgrades are cheap — design within current budgets, but
  measured burn approaching 80% of any cap triggers a tier-upgrade decision, not throttling.
  The budgets below are planning constraints, not hard walls.
- Config-driven per-source daily/monthly budgets; on breach: STOP calling, serve D1 data
  with staleness flag, alert CSuite group. Exhausted API = stale-but-honest, never broken.
- **Off-season polling pause** — saves both API credits and D1 rows (no games = no useful snapshots).

## B. CLOUDFLARE PLATFORM RISKS

### B1. D1 100K row-writes/day — backfill vs live sharing one budget
- Live needs ~1–6K/day. Backfill must target ≤90K/day to leave live headroom.
- Plan: backfill capped at 90K rows/day → 190K rows ≈ 2–3 days. Enforced in the script, not hoped for.

### B2. D1 storage 5 GB
- Team-level data fits with room to spare. The only balloon risk is `raw_payloads`
  (compressed CFBD responses, 5 seasons). Mitigation: store payloads only for endpoints
  feeding backfill (not the hourly refresh), gzip, prune >180 days. Budget: <2 GB.

### B3. D1 statement/batch limits
- Single SQL statement and per-request payload caps (~1 MB / 100 statements). Mitigation:
  chunked batch inserts (500–1,000 rows/statement), standard pattern, in the work order.

### B4. D1 as a new site dependency
- If D1 is unreachable, site must degrade, not die: serve last-known data from the
  Container's local cache with staleness banner. Read path wraps every query in
  try/cache fallback. Required in CTO's implementation.

### B5. Heavy backtest queries do NOT run in the Worker
- Backtests run locally (script exports D1 → local SQLite/DuckDB). Keeps Worker CPU
  limits and read-cap untouched. The site serves; the research happens on the ground.

## C. DATA-QUALITY RISKS (the ones that poison backtests silently)

### C1. Historical odds depth is shallower than games
- CFBD provides closing lines historically, but hourly line *history* only exists going
  forward (from our own snapshots). CLV-style backtests before ~2026 are closing-line-only.
- Accept this explicitly: 5 years of outcomes vs ~1 year of line movement. Plan backtests accordingly.

### C2. Season-boundary stat-definition drift
- CFBD changes/renames stats across seasons. Mitigation: `stat_key` vocabulary is versioned
  by a mapping table (`stat_map`: source_key → stat_key, valid_from_season). New CFBD name =
  one mapping row, zero migrations.

### C3. Team identity edge cases
- FCS opponents appear in FBS schedules; CFBD covers them — decide: store all games but
  tag `division`, backtests filter FBS-vs-FBS (or Jeff's preference). Team renames/rebrands
  are handled by stable CFBD team_id. Conference changes are observations, never edits.

### C4. Timezone normalization
- All timestamps stored UTC. Season/week boundaries derive from CFBD's week assignments
  (not local midnight math). Kickoff times stored as UTC, displayed ET/PT at the edge.

### C5. Duplicate/impossible rows
- Dedupe indexes on every table (per spec) + idempotent backfill (re-run is a no-op).
- Backfill checkpoint file is itself versioned — a partially-written checkpoint is discarded, not resumed.

### C6. Silent schema drift in raw sources
- CFBD adds/renames JSON fields without notice. `raw_payloads` archive = re-derivation
  insurance. Backfill validates row shapes against expectations and alerts on drift
  instead of writing garbage.

## D. OPERATIONAL RISKS

### D1. Backfill interrupts (reboot, update, sleep)
- Checkpoint per (season, endpoint, page). Resume is exact. Backfill runs as a script on
  the desktop; if the desktop sleeps, it resumes on wake. Long runs prefer the desktop
  awake or run under the container's cron-free environment — decision: run on desktop
  with power settings pinned for the 2–3 day window.

### D2. Predictions must precede games
- `model_predictions` written at the first poll after lines open, never post-hoc.
- Guard in code: reject a prediction row whose created_at > kickoff. Backtest integrity depends on this.

### D3. Model version comparability
- Auto-hash of composite config on every `rankings_daily`/`model_predictions` row.
- A weight change mid-season creates a visible version break — intended, documented.

### D4. Secrets hygiene
- Backfill script reads CFBD/Odds keys from env/file — never logged, never committed.
- The repo's secret-scanning (redact_secrets) stays on.

### D5. Rollback
- D1 is additive: if Phase 2 misbehaves, the app flag turns the write-path off and the
  site reverts to today's behavior instantly. Nothing existing is migrated or deleted.
- Reversibility statement for Jeff: worst case = drop the tables, flip the flag, back to
  exactly today. Render-era rollback discipline applies here too.

## E. COST SUMMARY (monthly, after Phase 2 is stable)
| Item | Cost |
|---|---|
| D1 (current usage) | $0 free tier |
| Workers/Containers paid plan | already paid |
| CFBD | already paid |
| The Odds API | $0 if option 2/3 chosen; ~$30/mo if paid tier chosen (verify) |
| PropLine | already paid |
| LLM tokens for nightly archive jobs | ~negligible (cron continuity + monitor mode) |

## F. DECISIONS REQUIRED FROM JEFF (blocking)
1. ~~**A1:** Odds API strategy~~ — RESOLVED: 20K/month confirmed, audit verifies burn.
2. ~~**C3:** FCS games~~ — RESOLVED (Jeff, Sep 21): **FCS data IN, store-and-tag, plus scope
   addition — an FCS rating** so early-season games vs FCS opponents are projected with
   data instead of blind defaults. **Jeff notes external FCS ratings likely exist**
   (Sagarin historically covers FCS; Massey composite aggregates FCS raters) — Research
   task: source-scan for a maintained, license-usable FCS rating; internal Elo is the
   fallback if nothing external is suitable. Impacts: teams table grows (~250 schools),
   composite gets a new stat_key (e.g. `fcs_rating`), backfill includes FCS game results.
3. ~~**D1:** backfill window~~ — RESOLVED (Jeff, Sep 21): desktop, see explanation below.

## G. SEQUENCING (durable plan)
1. CTO: API usage audit (blocking, dispatched) → Jeff approves budget plan
2. CTO: budget-enforcement layer + degraded-mode read path (B4, A4)
3. CTO: schema + store module + live write-path (spec sections 1–5)
4. Jeff approves budget plan → backfill runs 2–3 days, checkpointed, guarded
5. Backtest tooling built locally (B5), first reports on team-level data
6. Player stats: deferred, schema-ready, added later at paced writes
