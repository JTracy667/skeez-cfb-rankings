# QA BRIEF — Data freshness on skeezcfb-rankings.com

**Asked by:** Jeff, 2026-09-22
**Owner of the fix:** CTO
**Status of the fix:** NOT yet built. This brief starts verification early — see
"What to do now" vs "Acceptance criteria (for when it ships)".

---

## The incident

On 2026-09-22 the site watchdog alerted: the scheduled CFBD analytics anchor at
`2026-09-22T04:00:00Z` (21:00 PT, Mon) never produced a pull. Measured state:

```
GET /api/analytics/pull-status
  last_pull_utc         : 2026-09-21T04:29:18.122266+00:00
  last_pull_age_hours   : 39.46
  due                   : true
  most_recent_anchor_utc: 2026-09-22T04:00:00+00:00
  anchors               : Sun 21:00 PT, Mon 21:00 PT, Tue 21:00 PT, Wed 21:00 PT
```

So the site served analytics roughly **39.5 hours stale**. A pull did land at
`2026-09-22T19:57:14Z` — but only once the container was woken by ordinary
traffic, not by the anchor.

## What is already established (verify, do not assume)

CTO tested each of these; please reproduce independently rather than trusting it.

1. **The cron triggers ARE registered** for script `cfb-power-rankings`:
   `0 4 * * 1,2,3,4` and `0 5 * * 1,2,3,4` (both created 2026-09-21T01:45Z).
   Note the mapping: 21:00 PT == 04:00Z (PDT) / 05:00Z (PST), and cron days are
   UTC — Sun 21:00 PT is Mon 04:00Z. Both hours are listed so DST cannot miss the
   anchor.
2. **The ops gate is UP, not failing closed.** A token-less
   `POST /api/analytics/refresh-if-due` returns **401**, not 503.
3. **The scheduled handler exists** (`src/index.js:46`) and passes
   `X-Admin-Token: env.ADMIN_TOKEN`; `ADMIN_TOKEN` is wired into the container's
   `envVars` (`src/index.js:17`).
4. **The app self-heals on container start.** `app.py:478-516` — the daemon thread
   sleeps 10s, then runs `refresh_all()` and, if `weekly_analytics_due()`, pulls.
   This is the likely explanation for the 19:57:14Z pull landing seconds after the
   container was woken by traffic.
5. **NOT established — do not accept any story about this:** why the anchor did
   not wake the container. The CTO's "container needs 20 minutes idle" model is
   *inconsistent* with observed platform state (a live container instance reported
   `STATE: running` well past `sleepAfter`). Treat the causal story as unproven.

## WARNING — how to run this without invalidating your own results

**Every request to the site resets the container's idle timer.** The entire
question here is *when the container is awake vs asleep*. A verifier who polls
every few minutes keeps it permanently awake and will never observe a sleep, a
cold start, or a cron wake — and will then report "could not reproduce".

Therefore:
- **Do not** run tight polling loops against the site. Space probes minutes apart
  and record the exact timestamp of every request you make.
- Prefer **read-only, non-perturbing** evidence: `/api/analytics/pull-status`,
  `npx wrangler containers list` / `instances <app-id>`, and the D1 tables.
- Log your own request times so any "it was warm" result can be attributed to you
  rather than treated as a platform fact.
- If you need to observe a cold start, coordinate a quiet window — do not just
  retry until it looks cold.

## What to do NOW (verifiable today)

1. **Reproduce the staleness detection.** Explain exactly what `due` means in
   `/api/analytics/pull-status`, and confirm the anchor→UTC mapping above.
2. **Measure the real freshness SLA.** Over a window of your choosing, sample
   `last_pull_utc` and report how often analytics actually refreshed. The question
   worth answering: *what freshness does the site genuinely deliver in a normal
   week, independent of the cron's intent?*
3. **Test the self-heal-on-start claim** (`app.py:478-516`): does a container start
   reliably trigger a pull when due? This matters more than the cron, because it is
   what actually repaired the data.
4. **Independently reproduce the register/token/handler checks** in items 1-3 above
   (API queries + a token-less POST). Report raw output.
5. **Confirm the blast radius of the incident.** Did anything user-visible serve
   wrong data — rankings composites, win totals, or schedule projections computed
   from stale analytics? Report which pages/endpoints were affected and for how
   long. Do not speculate; check.

## Acceptance criteria (for when the fix ships — do not verify yet)

The agreed fix is three layers. Verify each independently.

**Layer 1 — inbound staleness guard (the one that solves it)**
- Fires a pull when the last successful pull is older than the threshold.
- Does **not** fire when data is fresh (no wasted CFBD quota).
- **Cannot stampede**: N concurrent requests produce at most one pull.
- Bounded: CFBD calls per hour under steady traffic stay within the documented
  budget.
- Env flag OFF restores exactly the previous behaviour.

**Layer 2 — hardened cron**
- `scheduled()` explicitly starts the container and retries on failure.
- Each attempt's outcome (status + body) is recorded **durably and queryably**
  — not only to `console.log`, which is NOT retro-readable (retained Workers Logs
  are not enabled; a telemetry query for the incident window returned 0 rows).

**Layer 3 — alerting**
- Staleness alerts at ~12h rather than 16h.

## Constraints — these are hard

- **Do not change model constants, weights, or calibration.** Freshness/ops work
  only.
- **Do not change what external data sources track or how parsers interpret them.**
  Reusing an existing parser in a new place is fine; editing its semantics is not.
- No deploy rights. Report findings; the CTO applies changes.

## Reporting format

Per item: **PASS / FAIL / INCONCLUSIVE**, with raw output and exit codes. If you
cannot reproduce something, say INCONCLUSIVE and state what you observed — that is
a genuinely useful result here, not a failure.