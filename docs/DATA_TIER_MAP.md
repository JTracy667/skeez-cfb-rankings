# Data Serving Architecture — moving the site off pull-on-demand

**Author:** CTO
**Date:** 2026-09-22
**Status:** PROPOSAL — needs Jeff + CEO sign-off before implementation
**Trigger:** the 2026-09-22 staleness incident (analytics served ~39.5h stale)

---

## 1. The problem in one paragraph

The site was built when it ran on Render: an always-on server, so "fetch data on
demand when a request arrives" was free — the server was awake whenever anyone
asked. That assumption died with the move to Cloudflare Containers, where the
service **sleeps after 20 minutes idle**. We kept the old fetching model anyway, so
today every page load is also a fetch trigger, and every refresh depends on the
container happening to be awake. When the anchor cron failed to wake it, the site
served stale data for 39 hours and nothing in the request path could repair it.

**The insight:** a persistent store (D1) separates *where data is fetched* from
*where data is served*. Once that is true, freshness stops being a property of
traffic and becomes a property of the ingester and the store — a missed cron
degrades nothing.

**The caveat:** this only holds for data that moves slowly. Live odds must stay
live. Getting the split wrong is worse than the current state, so the tier map in
§3 is the core of this document.

---

## 2. What is true today

| | Current behaviour |
|---|---|
| Compute | Cloudflare Worker + Container (FastAPI), `sleepAfter: 20m` |
| Fetching | In-request / daemon-thread, inside the container |
| Cache | In-memory + container filesystem — **ephemeral**, lost on sleep |
| Refresh trigger | Daemon thread (only alive while awake) + Worker cron at the anchor |
| Persistence | D1 (`cfb-history`) — currently an **append-only archive**, used for ledgers and backtests |
| Failure mode | Cron fails to wake container → no refresh until ordinary traffic arrives |

D1 exists and is well-populated (odds_snapshots, rankings_daily, closing_lines,
model_predictions, stat_observations, games, teams). **But its schema was designed
as an archive, not as a serving layer** — it has no read paths or indexes tuned for
the queries the pages actually make. That is real work, not a config change.

---

## 3. The tier map (the decision this doc is asking for)

**Slow tier — serve from D1.** Changes weekly or daily at most.

| Data | Source | Native cadence | Serve from | Max acceptable staleness |
|---|---|---|---|---|
| CFBD ratings (Elo / SP+ / FPI / recruiting) | CFBD | every 6h Sun-Wed (03/09/15/21 PT) | **D1** | 6h |
| Composite rankings | computed | on ratings change | **D1** (recompute on ingest) | same as ratings |
| Massey FCS ratings | Massey scrape | weekly snapshot (`week=0`) | **D1** | 7 days |
| Historical games / results | CFBD `/games` | daily | **D1** | 24h |
| Closing lines | odds provider | post-game | **D1** | n/a (historical) |
| Player starters / PPA | CFBD | nightly (2AM PT pre-warm) | **D1** | 24h |

**Ingestion window (Jeff, 2026-09-22):** CFBD ratings pull every 6 hours Sun-Wed
(03:00 / 09:00 / 15:00 / 21:00 PT). The ~3.5-day gap Wed→Sun is expected — CFBD
does not publish rating changes Thu-Sat. Quota impact: ~70 calls/month against
30,000 budget (0.2%). The same pull slot can piggyback game-result updates at no
additional quota cost.

**Fast tier — keep live, with a tight TTL.** These move within minutes; serving
them from the store would be a regression.

| Data | Source | Serve from | Max acceptable staleness |
|---|---|---|---|
| Game odds / spreads / totals | The Odds API | **live** | ~5 min |
| Line movement | The Odds API | **live** | ~5 min |
| PropLine props | PropLine | **live** | ~5 min |
| Win totals (season O/U) | sportsbook | live | ~1h |
| Injuries | CFBD | live, cached | ~6h |

**The rule that must never be broken:** odds and props are never served from the
slow tier. A stale line served as live is worse than an outage, because it looks
correct and it is not. Every served payload should carry the timestamp of the data
it was built from, so staleness is always visible rather than inferred.

**Prediction-level freshness gate (Jeff + CEO, 2026-09-22):** Every composite
prediction must carry the age of its **oldest input** (SP+, Elo, FPI, recruiting).
If any input exceeds the tier's max acceptable staleness, the prediction endpoint
must either refuse to serve or mark the response `STALE_INPUTS` with per-input age.
This prevents a confident-looking score prediction built on silently old ratings
from reaching a user.

**Ingestion data-quality guard (CEO, 2026-09-22):** if a pull returns suspicious
data (team count drops sharply, all-zero ratings, missing conferences), do NOT
overwrite the last good snapshot — log the anomaly and alert. Bad data
overwriting good data is worse than stale data.

---

## 4. The constraint that shapes the rollout

**The parsers live in `app.py`, inside the container image.** So "ingest on a
schedule instead of in the request path" requires running that same code somewhere
that is always available — we cannot simply move it into the Worker.

This pattern already exists here: the **2AM PT cache pre-warm cron on this box**
force-refreshes CFBD starters/PPA into `data/` and a heartbeat. So the mechanism is
proven; the change is scope, not invention.

**Hard limit:** the parsers are **reused verbatim**. What the sources track and how
parsers interpret them is Research's territory. This proposal changes *where and
when* code runs, never *what it means*.

---

## 5. Phased plan

**Phase 0 — staleness guard (NOW, independent of everything below).**
Any request checks the age of the last successful pull; if stale, kick one
background pull behind a lock. Fixes today's pain without touching the
architecture. *Ships first whether or not the rest is approved.*

**Phase 1 — data-tier map ratified.** This document. Jeff + CEO agree the split in
§3. No code changes.

**Phase 2 — D1 read paths.** Add serving-shaped read queries + indexes for the
slow tier. Pure addition; the archive keeps writing as it does now.

**Phase 3 — slow tier reads from D1.** Pages read the slow tier from D1. Live pulls
remain for the fast tier only.

**Phase 4 — ingestion decoupled.** Move slow-tier ingestion to a scheduled job
(pattern: the existing pre-warm cron), so ingestion no longer depends on the
container being awake.

**Phase 5 — freshness SLA + alerting.** The watchdog alerts on breach of §3's
thresholds, per tier.

---

## 6. BUILD STAMP

**Feasibility:** Phases 0, 2, 4 are ordinary engineering on code we own, with a
working precedent for Phase 4. Phase 3 depends on Phase 2 being correct — that
ordering is not optional. No model, calibration, or source-semantics changes are
required at any phase.

**Rollout:** one phase per deploy. Phase 2 is additive (safe by construction).
Phase 3 is the risky one and ships behind an env flag so the old path can be
restored without a code revert. Phase 0 is flag-gated too.

**Rollback:** per-phase. Flag off → previous behaviour in one deploy. Config-level
rollback = re-point at the previous known-good image tag (Render is decommissioned;
that is our rollback path). D1 is additive and never destructive, so the archive
remains intact and usable even if serving moves back to live pulls.

**Cost:** D1 reads are billed; D1 is already on Workers Paid with a 50M rows/month
write allowance and reads are cheap at this volume. CFBD consumption *drops* for
the slow tier, since page loads stop triggering pulls.

---

## 7. Risks

| Risk | Mitigation |
|---|---|
| Stale odds served as live | Hard rule in §3; every payload carries its source timestamp; QA explicitly tests this |
| Prediction served from stale inputs | Prediction freshness gate (§3): `STALE_INPUTS` flag or refuse; per-input age stamped on every prediction |
| Bad data overwrites good snapshot | Ingestion data-quality guard (§3): anomaly check before write; alert instead of overwrite |
| D1 unavailable during a request | Slow-tier pages fall back to last cached edge copy (short TTL); fast tier unaffected (live path) |
| D1 read latency slows page loads | Cache at the edge with a short TTL; measure before/after on the real payload sizes |
| Silent divergence between D1 and live | Per-field "as of" stamp surfaced in `/api/health` and page payloads |
| Ingestion job dies unnoticed | Same heartbeat + age-check pattern as the existing pre-warm cron; watchdog alerts |
| Scope creep into model/source semantics | Explicitly out of bounds; parsers reused verbatim |

---

## 8. Open questions for Jeff / CEO

1. **Approve the tier split in §3?** This is the decision that matters — it changes
   how most of the site sources its data.
2. **Phase 0 now, rest later?** Recommended: yes. Phase 0 alone removes the 39-hour
   failure mode.
3. **Where should scheduled ingestion run long-term** — this box (relying on a
   local cron, as the pre-warm already does), or scheduled inside Cloudflare?
   Locally is simpler today but couples site freshness to a desktop being on.
   *CEO recommendation (2026-09-22):* start local (pattern proven), plan
   Cloudflare-side scheduled ingestion as Phase 4b — removes the desktop-uptime
   dependency without blocking the rest of the rollout.