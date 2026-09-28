# CFB Power Rankings — DATA FLOW (authoritative)

> **READ THIS BEFORE TOUCHING THE SITE.** It answers "where does this number come
> from, where is it stored, and what reads it" for every dataset. If you change a
> producer or a reader, update this file **in the same commit**.

Last verified: 2026-09-27, live build **v51** (`code.marker = v51-serve-from-d1`).

---

## 0. The three storage tiers

| tier | what | durability |
|---|---|---|
| **D1** (database `cfb-history`) | system of record | **durable** |
| **Container disk** | the image's baked files (`data/*.json`, COPY'd by the Dockerfile) | **EPHEMERAL** — discarded on every recycle (`sleepAfter` 5m idle) |
| **In-process memory** | TTL caches (analytics 60s, odds, etc.) | lost on recycle |

**The rule (Jeff): all data goes to D1, and the site READS D1.** The disk is a
bootstrap/fallback only — never the source of truth.

---

## 1. Serving path — what a visitor's page load actually reads

| page / endpoint | reads | notes |
|---|---|---|
| `/api/analytics` | `_served_analytics()` → **D1 first** (v51) | disk file only as fallback |
| `/api/rankings` | `_build_team_map()` → `_served_analytics()` | composite computed per request, 60s cache |
| `/schedule`, `/win-totals` | same `_build_team_map()` | |
| `/api/matchup` | `_served_analytics()` | |
| lines / odds columns | `odds_snapshots` + in-memory cache | refreshed hourly |

**Before v51** the serving path read **only** `data/cfbd_analytics.json` — the file
baked into the image. That is why a week of weekly pulls was invisible to visitors.
**Do not regress this**: any NEW endpoint must read D1, not the disk file.

---

## 2. Producer → store → reader, per dataset

| dataset | producer | stored where | key | durable | read by |
|---|---|---|---|---|---|
| team analytics metrics (SP+, SRS, Elo, FPI, talent, experience, efficiency, PPA, havoc…) | `fetch_live_analytics()` → `_store_team_analytics()` → `snapshot_team_analytics()` | **D1 `stat_observations`** (`subject_type='team'`) | `(subject_id, season, stat_key, week)` **UNIQUE** | yes | `_served_analytics()` |
| team identity (name, mascot, conf, emoji, streak) | local teams source | container disk `data/cfbd_analytics.json` + D1 `teams` (684) | name / team_id | **disk is ephemeral** | merged into the served payload |
| games / schedule / results | CFBD `/games` | **D1 `games`** (21,204) | `game_id` | yes | schedule page, records overlay |
| odds / lines | The Odds API + PropLine | **D1 `odds_snapshots`** (181,982), `closing_lines` (7,184) | `game_id`, book, ts | yes | lines columns |
| weather | CFBD weather endpoint | **D1 `weather_snapshots`** (40,782) | `game_id`, `poll_ts` | yes | weather badges |
| injuries | injuries sync | **D1 `injury_snapshots`** (402) | — | yes | injury flags |
| derived boards (schedule / rankings / win_totals) | board builders | **D1 `slate_cache`** (4) | `kind, season, week` | yes | the three pages |
| served payload states | `snapshot_served_state()` | **D1 `served_snapshots`** (128) | — | yes | change detection |
| API usage / quota | call counter | **D1 `api_usage`** (32) | `bucket, period, source` | yes | quota guards |
| last analytics pull ts | pull path | **D1 `app_state`** (`last_analytics_pull_ts`) | `key` | yes | freshness guard |
| freshness / boot events | app | **D1 `freshness_events`** (201) | `id` | yes | diagnostics |
| model predictions | `snapshot_predictions()` | **D1 `model_predictions`** (272) | — | yes | backtests |
| raw API payloads | `snapshot_raw_payload()` | **D1 `raw_payloads`** | — | yes, but **only 6 rows** | — |
| **player-level data** | *(nothing writes it)* | **D1 `players` — 0 ROWS** | — | **empty** | — |
| backtest runs | backtest tooling | **D1 `backtest_runs`** (12) | — | yes | backtests |

---

## 3. Traps — every one of these has already bitten us

1. **The container disk is ephemeral.** A runtime write to `data/*.json` is lost on
   recycle. Durable = write D1, or regenerate the file in the repo and ship an image.
2. **D1-first is required on every serving path.** v51 wired `/api/analytics`,
   `_build_team_map()` and `/api/matchup`. New endpoints must do the same.
3. **`stat_observations` is an UPSERT on `(subject, season, stat_key, week)`.** Within
   a week, every pull **replaces** the previous value — last write wins, and
   intermediate pulls leave **no trace**. You therefore **cannot audit** whether a
   given pull archived. Real pull history needs a schema change (append a `pull_id`).
4. **`@_guard` swallows archive failures.** Every `snapshot_*` in `d1_write_path.py`
   catches exceptions, returns `0`, and prints "failed (site unaffected)". A broken
   archive is **silent**. Never trust "no error" — verify with row counts.
5. **`stat_observations` holds numerics only.** mascot / conf / emoji / streak cannot
   live there; they stay on disk. Any D1 read must merge, not replace.
6. **`d1_write_path.enabled()` gates READS too**, keyed off the `D1_WRITE_ENABLED` env
   var. Unset → D1 reads silently return `[]` and you fall back to disk without warning.
7. **The freshness guard's stamp only advances on an *anchor-flagged* pull**
   (`app_state.last_analytics_pull_ts`), so after a recycle the site can serve the
   build-time copy for up to 12h.
8. **`wrangler deploy` is not live until the warm container recycles** (~6m typical,
   up to ~12m). Verify a **code-level** marker (`code.marker` in `/api/health`) —
   never `build` alone, which is handed to the still-warm old instance.

---

## 4. Gaps and the plan

> **Measured audit of this whole map: `docs/DATA_SYMMETRY_AUDIT.md`** — write/read
> symmetry per table, the disk tier that is still served, and findings F1–F8 ranked.
> Re-runnable: `scripts/audit_d1_symmetry.py` (serve path) and
> `scripts/audit_d1_buildpath.py` (board build path, writes stubbed).

| # | gap | impact | plan |
|---|---|---|---|
| 1 | Intra-week overwrite in `stat_observations` | no revision history; unauditable pulls | add `pull_id`/`recorded_at` to the key → append-only. **Jeff's call (row growth ~2×/day)** |
| 2 | Raw API payloads not retained (6 rows) | can't recompute metrics from source | archive the pull's raw JSON per pull |
| 3 | `players` table empty | no player-level persistence | decide what's actually needed |
| 4 | `@_guard` silent failures | a dead archive looks healthy | log failures to `freshness_events` + alert |
| 5 | No parity test between D1 and what's served | exactly how the v49/v50 bug hid | **contract test: served payload == D1 latest** |
| 6 | Reads gated on `D1_WRITE_ENABLED` | misleading coupling | split into a read flag |
