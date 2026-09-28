# OPERATIONS — skeezcfb-rankings.com

**READ THIS FIRST.** It is the operations map for the live site: how it runs, how it
deploys, where its data actually lives, and how to prove each claim. The point is that
a fresh session can act without re-deriving any of this from source.

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
| Live build | **v49** — `/api/health` → `build`, proven by `code.marker` |
| Code marker | `v49-board-rebuild-fix` |
| Image tag in `wrangler.jsonc` | `cfb-power-rankings:v49` |
| Rollback tag | previous known-good tag — `scripts/cfb_deploy.sh --rollback vN` |
| Last verified | 2026-09-27 (CTO) |
| Known-stale docs | `docs/SESSION_HANDOFF.md` (state as of Sep 23 — do NOT trust its state), `CLOUDFLARE_DEPLOY.md` (says `sleepAfter 20m`) |

Verify in one line:
```bash
curl -s https://skeezcfb-rankings.com/api/health   # status ok + build + code.marker + budgets
```

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
  for a probe that *detects* the release. The intended fix is to make the probe
  *trigger* the pull (release-gated + settle-gated), keeping the anchors as a backstop.
  Blocker: the ETag baseline must live in **D1** (it is currently a repo file, i.e.
  ephemeral in the container). See `docs/CFBD_API_MAP.md` §3c/§3d.
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
- Gated (8): `rankings/refresh`, `refresh`, `analytics/refresh-if-due`,
  `schedule/update`, `injuries/sync`, `injuries/override`, `record/ingest`,
  `record/repair-ats` — plus `analytics/fetch` and the admin-only `/api/matchup`.
- Deliberately **NOT** gated: **`/api/analytics/fetch`** and **`/api/schedule/fetch`** —
  a public page cannot hold a secret; they are throttled via a cached payload instead.

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