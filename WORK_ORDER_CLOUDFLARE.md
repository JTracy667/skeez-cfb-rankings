# WORK ORDER: Cloudflare Containers migration — skeezcfb-rankings.com

**From:** CEO (Hermes default), at Jeff's direction, Sep 20 2026
**To:** CTO
**Priority:** High — Render free-tier 512MB OOM is the driving problem. Budget approved: Workers Paid ($5/mo).
**Rollback requirement:** Render stays live and untouched until final cutover verification passes. Rollback = DNS revert, < 5 min.

## State of play (verified by CEO)

- `wrangler.jsonc` is complete: container `CFBPowerRankings`, image `v6` already pushed to
  `registry.cloudflare.com/90c2c31beec12cb7de1c249ade1eb773`, DO binding + migration present.
- `src/index.js` routing Worker exists. `CLOUDFLARE_DEPLOY.md` documents the path.
- Render (`render.yaml`) remains the live deploy: hourly refresh (`REFRESH_INTERVAL_SECONDS=3600`),
  background scheduler thread in app.py, data seed baked into image.
- **Discrepancy to resolve:** notes in CLOUDFLARE_DEPLOY.md say `max_instances: 2`, wrangler.jsonc
  says `1`. Pick deliberately (see step 4) and make config + notes agree.

## Status (CTO run, Sep 20 2026)

Steps 1–5 COMPLETE, verified:
- Pre-flight: Workers Paid active (container app deployed + running), docker 29.6.2 green,
  `wrangler whoami` = account 90c2c31beec12cb7de1c249ade1eb773 (same as zone).
- v7 built + pushed + deployed (deployments list shows Sep 20 20:00Z upload, image v7 pushed).
- max_instances kept at 1, class `basic` (0.25 vCPU / 1 GiB) — see reasoning below.
- Staging verified on https://cfb-power-rankings.jeff-90c.workers.dev:
  - `/api/health` → `{"status":"ok","teams":25,"cache_ttl":300}` http 200
  - Cold-start after 6.5 min idle: 248 ms http 200 (no user-visible downtime)
  - `/api/rankings` 200 (week Sep 20 2026, 25 teams), `/api/odds` 200 (source=propline,
    proves PROPLINE_API_KEY reached container), `/api/schedule/current-week` 200
  - POST `/api/rankings/refresh` → `{"status":"refreshed","source":"espn","teams":25}`
  - GraphQL analytics: 0 errors after 20:08Z; the 6 exceptions 20:01–20:05Z were during the
    deploy/secret-change window itself. No OOM/restart signature. Memory check: container
    health errors empty, 1 GiB provisioned vs Render's 512 MB — migration goal met.
- Secrets: CFBD_API_KEY, PROPLINE_API_KEY, THE_ODDS_API_KEY all present (`wrangler secret list`).
- Instance sizing decision: keep max_instances=1 + basic. Traffic is tiny (24 invocations over
  75 min in analytics), cold-start measured 248 ms — a second instance would double cost for
  nothing. Revisit only if real traffic shows cold-start gaps.

**BLOCKED — Step 6 cutover (needs Jeff or a new token):**
The CLOUDFLARE_API_TOKEN (cfat_ account token) has Workers permissions but NOT Zone→DNS Edit.
- PUT /accounts/…/workers/domains for apex+www → error 100117 "Hostname already has externally
  managed DNS records (A, CNAME). Delete them first" — deleting the Render A record needs DNS edit.
- GET /zones/…/dns_records → 10000 Authentication error.
Staging remains fully live and Render untouched, so nothing is broken. Fix: either Jeff deletes
the existing `skeezcfb-rankings.com` A record in the dashboard and re-adds both hostnames as
Worker custom domains (Workers & Pages → cfb-power-rankings → Domains & Routes), or issues a
token with Zone→DNS Edit and the CTO finishes cutover + verification.

## Steps

1. **Pre-flight (blockers first)**
   - [ ] Confirm the CF account (the one holding the skeezcfb-rankings.com zone, account id
        `90c2c31beec12cb7de1c249ade1eb773`) has **Workers Paid** enabled. Containers do not run on
        free. If not subscribed: tell Jeff — do not subscribe on your own.
   - [ ] `docker info` green (Docker Desktop running).
   - [ ] `npx wrangler whoami` — logged into the SAME account as the zone. Wrong-account deploys
        are the classic silent failure here.

2. **Rebuild + push fresh image** (v6 is from an earlier code state; rebuild so the image matches
   current `app.py`)
   - [ ] `docker build -t cfb-power-rankings:v7 .`
   - [ ] Update `wrangler.jsonc` image tag to v7, bump `max_instances` per step-4 decision.
   - [ ] `npx wrangler deploy` (builds + pushes + deploys container + worker).

3. **Staging verification on workers.dev** (BEFORE touching DNS)
   - [ ] `curl https://cfb-power-rankings.<subdomain>.workers.dev/api/health` → `{"status":"ok",...}`
   - [ ] Pull one page (index), one data API, and confirm the odds/refresh background thread wakes
        (check logs via `npx wrangler tail` after ~1 interval, or hit `/api/refresh` manually).
   - [ ] **Cold-start check:** let it sleep 5+ min, hit it again, record cold-start latency. If a
        cold start is slow enough to look like downtime to a user, consider `sleepAfter` tuning or
        keep-alive ping, and say so in the report.
   - [ ] **Memory check:** `wrangler tail` + container metrics — confirm no OOM/restart under a
        full refresh cycle. This is the whole point of the migration; a CF deploy that OOMs like
        Render is a failed migration, report it honestly if so.

4. **Instance sizing decision** (you own this call, justify it in the report)
   - `basic` class vs higher class — pick based on observed memory in step 3.
   - `max_instances`: 1 is cheapest and fine for this traffic; 2 avoids cold-start gaps. Decide
     with data, note the reasoning.

5. **Secrets** (Worker secrets, never in git)
   - [ ] `wrangler secret put CFBD_API_KEY` / `PROPLINE_API_KEY` / `THE_ODDS_API_KEY`
   - [ ] Verify all three actually reached the container (a missing key shows up as a refresh
        failure, not a deploy failure).

6. **Domain cutover** (only after step 3 fully passes)
   - [ ] Add `skeezcfb-rankings.com` as Worker custom domain (Workers & Pages → worker →
        Settings → Domains & Routes). Zone is already on Cloudflare (currently DNS-only to Render).
   - [ ] Add `www.skeezcfb-rankings.com` too — it historically had no record and 404'd for
        bookmarked users; fix it in the same cut.
   - [ ] Verify: `curl -I https://skeezcfb-rankings.com/api/health` and the www form + redirect.
   - [ ] Leave Render service running (paused is fine) for 48h as the instant-rollback path.
        Rollback = remove Worker custom domain, restore DNS record to Render.

7. **Report back**
   - Post-deploy report in the group: container class/instances chosen + why, cold-start latency,
     memory under load, secret verification, cutover timestamps, rollback state.
   - Do NOT tear down Render until Jeff signs off after 48h.

## Phase 2 (separate work order — do NOT do in this migration)
D1 database for stats history (free tier: 5M reads/day, 1GB) to take data volume off
container memory. Requires app changes — design first, get sign-off.

## Hard rules
- No secrets in git or chat. Worker secrets only.
- Render is production until cutover is verified. Never leave the site in a state where
  neither Render nor CF is serving.
- Every claim in the final report needs a receipt (curl output, log line, or metric).
