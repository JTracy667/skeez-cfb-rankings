# Skeez CFB Rankings — Cloudflare Deploy

Package the FastAPI app as a **Cloudflare Container** (Docker image on Cloudflare's
network) with a thin Worker routing all traffic to it. No code changes needed —
the app runs exactly as it does locally. Deployed domain: **SkeezCFB-Rankings.com**

## Files

| File | Purpose |
|---|---|
| `Dockerfile` | Builds the app image (python:3.11-slim + FastAPI + data seed) |
| `src/index.js` | Worker that routes all requests to the container on port 8003 |
| `wrangler.jsonc` | Wrangler config: container class, Durable Object binding |
| `package.json` | wrangler + @cloudflare/containers dev deps |
| `.dockerignore` | Excludes secrets/caches from the image |

## Prerequisites

- Docker Desktop **running** (`docker info` must succeed)
- Node.js 18+ (bundled wrangler runs via `npx` if needed)
- A Cloudflare account with **Workers Paid plan** (Containers requires it)
- `wrangler` CLI: `npm i` then `npx wrangler login`

## Deploy

```bash
cd C:\Users\jtracy\dev\cfb-power-rankings
npm i                    # install wrangler + containers SDK
npx wrangler login       # one-time browser auth
# Set your API keys as Worker secrets (never commit .env!)
npx wrangler secret put CFBD_API_KEY
npx wrangler secret put PROPLINE_API_KEY
npx wrangler secret put THE_ODDS_API_KEY
# Build + push the container image FIRST, then deploy the Worker.
# `wrangler deploy` alone does NOT build the image — rolling out a tag that is
# not in the registry fails with IMAGE_REGISTRY_DOESNT_CONTAIN_IMAGE.
npx wrangler containers build . --tag cfb-power-rankings:v<N> --push
npx wrangler deploy
```

Non-interactive alternative to `wrangler login`: export `CLOUDFLARE_API_TOKEN`
(account-scoped token with Workers + Containers write). `wrangler whoami` must
show account `90c2c31beec12cb7de1c249ade1eb773` — the same account as the zone.

Your app will be live at `https://cfb-power-rankings.<your-subdomain>.workers.dev`.
Add your custom domain in the Cloudflare dashboard: **Workers & Pages → your worker
→ Settings → Domains & Routes → Add** `SkeezCFB-Rankings.com`.

## Verify / Manage

```bash
npx wrangler containers list        # container instances + status
npx wrangler containers images list # images in the Cloudflare registry
npx wrangler tail                   # live logs
```

## Local testing (before deploy)

```bash
docker build -t cfb-power-rankings:test .
docker run -d --name cfb-test -p 8004:8003 \
  -e CFBD_API_KEY=x -e PROPLINE_API_KEY=x -e THE_ODDS_API_KEY=x \
  cfb-power-rankings:test
curl http://localhost:8004/api/health   # {"status":"ok","teams":25,...}
```

## Notes

- **Ports**: app listens on 8003 → `defaultPort = 8003`, `pingEndpoint` health-checks
  `/api/health`.
- **Secrets**: envVars in `src/index.js` pull from Worker secrets (set via
  `wrangler secret put`), passed into the container at start.
- **Data**: `data/` is baked into the image as a cold-start seed (687 teams).
  Runtime writes (odds cache, ESPN cache) live on the container's ephemeral disk
  and are re-fetched on restart — no persistent storage configured.
- **Scaling**: `max_instances: 1` — matches wrangler.jsonc (single DO singleton; one warm
  instance serves all traffic, cache stays coherent). Bump only if cold-start gaps hurt.
  Containers sleep after 20m idle (`sleepAfter` in src/index.js) and cold-start on next request.
- **CORS**: pages and API are served same-origin through the Worker (no CORS
  needed). If you add a custom domain or a Pages frontend, set the `CORS_ORIGINS`
  env var (comma-separated) — the app's allowlist reads it at startup.

## Refresh parity — the analytics anchor (Sep 20 2026)

The app re-pulls CFBD ratings (SP+/Elo/FPI/talent) at **Sun/Mon/Tue/Wed
21:00 America/Los_Angeles** (`_ANALYTICS_ANCHORS`, `_PT_TZ` in app.py). Jeff's
correction: the anchor is 9pm PACIFIC; the original 21:00 ET fired at 6pm PT.

This host cannot rely on app.py's background scheduler thread for that:
`sleepAfter = "20m"` stops the thread whenever traffic stops, so a pull would
happen only when something woke the container. Hence:

- `src/index.js` has a `scheduled()` handler that POSTs
  `/api/analytics/refresh-if-due` (idempotent, anchor-gated — same check the
  in-process scheduler uses, so it never double-pulls).
- `wrangler.jsonc` registers crons **`0 4 * * 1,2,3,4`** and **`0 5 * * 1,2,3,4`**
  (21:00 PT == 04:00Z PDT / 05:00Z PST; both hours are needed or the anchor is
  missed for half the year; cron days are UTC — Sun 21:00 PT is Mon 04:00Z).
- `GET /api/analytics/pull-status` exposes `last_pull_utc` / age / `due` so both
  environments can be compared directly instead of inferring freshness from
  cache-build timestamps inside payloads.

**A new image only reaches a sleeping-or-new instance.** After
`wrangler containers build --push` + `wrangler deploy`, a warm instance keeps
serving the OLD image until it recycles (20m idle); verify with
`/api/analytics/pull-status`, which only exists in v10+.

**Cloudflare in front of the Worker blocks header-poor clients** (403
`error code: 1010`) — requests with the bare `Python-urllib` signature are
rejected while curl/browser requests pass. Any monitoring or script client must
send a real `User-Agent` + `Accept`. Render (DNS-only) has no such filter, so
this is a behaviour change to plan for at cutover.
