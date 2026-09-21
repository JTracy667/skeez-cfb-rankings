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
cd C:\Users\JeffTracy\Desktop\cfb-power-rankings
npm i                    # install wrangler + containers SDK
npx wrangler login       # one-time browser auth
# Set your API keys as Worker secrets (never commit .env!)
npx wrangler secret put CFBD_API_KEY
npx wrangler secret put PROPLINE_API_KEY
npx wrangler secret put THE_ODDS_API_KEY
# Build image + push + deploy
npx wrangler deploy
```

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
