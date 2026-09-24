# Load-time analysis — the 2026-09-24 watchdog alert on `/api/win-totals`

**Trigger:** `cto-cfb-site-watchdog` reported `/api/win-totals -> unreachable` at
2026-09-24 14:51Z (07:51 PT). Every other probe in the same sweep (`/`, `/analytics`,
`/schedule`, `/api/health`) passed within its 60s timeout.

**Verdict:** the alert was real, the endpoint was never down, and the cause was a
missing cache — not the v35 deploy.

## What the watchdog measures

`profiles/cto/scripts/monitor_cfb_site.py` probes four pages plus `/api/health` in
sequence with `urllib` and a **60s read timeout** (`get(url, timeout=60)`). A
non-200 *or an exception* (including a timeout) is reported as `unreachable`.
So a single request that takes longer than 60s is indistinguishable from an outage.

## Root cause (measured, not inferred)

`cfbd_shared.team_aliases()` had **no cache**: every call issued a live
`GET /teams` against CFBD.

`compute_win_totals()` resolves the FCS opponent of each game through
`fcs_composite_for() -> _fcs_rank_for() -> _team_id_for() -> team_aliases()`.
The 2026 slate has 127 FBS-vs-FCS games, so **one Win Totals rebuild made 128 live
CFBD round-trips**.

`scripts/profile_win_totals.py` (cProfile, full core, warm disk caches):

```
1,467,079 function calls in 49.316 seconds
   ncalls  cumtime  function
        1   49.317  compute_win_totals
      128   48.813  cfbd_shared.cfbd_get
      127   48.655  app.fcs_composite_for -> _fcs_rank_for -> _team_id_for
      127   48.622     cfbd_shared.team_aliases
      129   48.471        httpx._client.get
     3299   47.736           {method 'read' of '_ssl._SSLSocket'}
```

**47.7 of 49.3 seconds was socket reads.** It is network-bound, not CPU-bound —
which is why the 1/4 vCPU `basic` instance type was never the main problem.

## Why it surfaced as a timeout only once

`_WIN_TOTALS_CACHE` is in-process with a 1-hour TTL. When the TTL expires, the
**first** request pays the whole rebuild. Measured on prod (each preceded by a
genuine idle window):

| Probe | Result |
|---|---|
| `/api/win-totals` after 5.5 min idle | 200, 325,271 B, **0.80s** (cache still warm) |
| `/api/win-totals` after 12 min idle | 200, 325,271 B, **0.70s** (cache still warm) |
| `POST /api/schedule/fetch?week=4` on a cache miss | 200, 80,353 B, **5.66s** |
| `/api/record` (warm) | 200, 277,692 B, **2.49s** |

The instance does **not** recycle at `sleepAfter=5m` in practice, so the 1-hour
cache usually survives; the rebuild lands roughly hourly, and whenever it lands on
a busy core it can cross 60s. That is the one failure in the monitor's state.

## Not a v35 regression

`scripts/probe_cold_compute.py`, same machine, warm disk caches, cold memory cache:

| | `compute_win_totals` | `api_schedule_fetch` |
|---|---|---|
| v34 (`38a026e`, worktree) | 46.78s | 4.05s |
| v35 (`8915c15`) | 40.47s | 4.51s |

Same defect in both; v35 is not slower. (Do not compare a fresh worktree against
the working tree without copying the gitignored `data/*.json` caches into it — the
first comparison run in this investigation was invalid for exactly that reason, and
reported a phantom 57.54s `api_schedule_fetch`.)

## The fix

`cfbd_shared._teams_cached(year)` — an in-process TTL cache (6h) around CFBD
`/teams`, used by `team_aliases()` and `teams_by_name()`. `/teams` is a static
list; re-downloading it once per lookup was pure waste.

Same run, same machine, fix applied:

```
cfbd season games: 3679 games in 1.31s
compute_win_totals (cold cache): 1.99s for 138 teams     (was 52.15s)
api_schedule_fetch week=4 (cold cache): 1.34s, 71 matchups   (was 3.74s)
```

**Numbers unchanged:** `PROBE_DUMP` payloads before vs after the fix are
byte-identical apart from the `generated` timestamp.

Side benefit: ~128 CFBD calls per rebuild eliminated — on an hourly rebuild
cadence that is roughly **3,000 calls/day** of the 30,000/day CFBD budget spent
re-fetching a static list. `cfbd_shared` is shared with the D1 backfill, so that
path benefits too.

## Still open (needs Jeff's call — Tier 3 spend)

1. **`instance_type: basic` = 1/4 vCPU, 1 GiB** (`wrangler.jsonc`). Cloudflare's
   smallest. `standard-1` = 1/2 vCPU / 4 GiB, `standard-2` = 1 vCPU / 6 GiB. The
   profile shows the hot path is I/O-bound, so this is no longer the first lever —
   but it still matters for the ~36s background refresh competing with requests.
2. **`data/odds_snapshots/` is `COPY`'d into the image** (~50MB/day). Add a
   `.dockerignore` line next time the build is touched.
3. **Schedule page makes 6-8 sequential round trips** (`/api/schedule/weeks`,
   `/api/schedule/fetch`, `/api/best-bets`, `/api/best-bets/record`,
   `/api/line-movements`, `/api/record`, `/api/schedule/current-week`; note
   `/api/record` is fetched twice). Total 4.7s warm. Several are independent and
   could be parallelised; `/api/record` at 277KB could be trimmed.