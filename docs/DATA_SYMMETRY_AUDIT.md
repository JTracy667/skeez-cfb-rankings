# D1 WRITE/READ SYMMETRY AUDIT — 2026-09-28

**Why this exists:** the v49/v51 bug (a week of weekly pulls never reaching the site
because the serving path read the image file instead of D1) was not a one-off — it was a
*class* of bug: data written somewhere, served from somewhere else, with nothing
asserting the two agree. This audit walks every dataset and reports where each one is
written, where it is read, and whether the live path actually connects them.

Read `docs/DATA_FLOW.md` for the per-dataset map; this is the audit of that map.

---

## 1. Method — and its limits

Two instruments, both in the repo and re-runnable:

| script | measures |
|---|---|
| `scripts/audit_d1_symmetry.py` | every SQL statement the app issues while **serving** each public endpoint (wraps `d1_store.query`/`query_full`) |
| `scripts/audit_d1_buildpath.py` | the same for the **board BUILD** path (`get_rankings(force=True)`, `load_schedule()`, `compute_win_totals(force=True)`), with the board writers stubbed to no-ops so it cannot touch production |

```bash
set -a; . ./.env; set +a
export D1_WRITE_ENABLED=1
python scripts/audit_d1_symmetry.py
python scripts/audit_d1_buildpath.py
```

**What this method CANNOT see — and I checked the gap separately:**

- **Disk-file reads.** The capture intercepts D1 SQL only. A `json.load(open(...))` is
  invisible to it. The disk tier is therefore reported from the `*_FILE` constants and
  their call sites in `app.py`, not from the capture.
- **Writes made by cron/ops scripts** rather than the app (see §4).
- **The board build when a board is already warm** — that is why the build path is
  measured by forcing a rebuild.

---

## 2. The three tiers actually in use at serve time

| tier | durability | in use on the serving path? |
|---|---|---|
| **D1** (`cfb-history`) | durable | **yes** — 5 tables |
| **Container disk** (`data/*.json`) | **ephemeral** (reverts on recycle, `sleepAfter` 5m) | **yes** — at least 11 files |
| in-process memory | lost on recycle | yes (TTL caches) |

**Both durable and ephemeral storage are live at the same time.** That is the structural
condition that produced the v50 incident, and it is still true elsewhere (§5, F2).

---

## 3. D1 tables — measured

### 3a. Read on the serving path (MEASURED, SQL capture)

| endpoint | D1 tables read |
|---|---|
| `/` `/analytics` `/schedule` `/win-totals` | `app_state` only (pages are static HTML; the JS then calls the APIs) |
| `/api/health` | `app_state`, `api_usage` |
| `/api/rankings` | `app_state`, `slate_cache` |
| `/api/schedule` | `app_state`, `slate_cache` |
| `/api/analytics` | `app_state`, **`stat_observations`**, `teams` |

### 3b. Read on the board BUILD path (MEASURED)

| build | D1 tables read |
|---|---|
| `get_rankings(force=True)` | `stat_observations`, `teams`, `api_usage` |
| `load_schedule()` | **none** — reads `data/week_schedule.json` from **disk** |
| `compute_win_totals(force=True)` | **none** — no D1 access |

### 3c. Full table verdict

| table | rows | written by the LIVE app | read by the LIVE app | verdict |
|---|---|---|---|---|
| `stat_observations` | 92,406 | `snapshot_team_analytics` | `load_team_analytics` | **OK** (the v51 fix) |
| `app_state` | 3 | `save_state`, `set_last_pull_ts` | `get_last_pull_ts`, `load_state` | OK |
| `slate_cache` | 4 | `store_slate` | `load_slate` | OK |
| `api_usage` | 32 | via `budget.flush()` | `/api/health` | OK |
| `teams` | 684 | **NONE** | `daily_rankings`, `load_team_analytics` | **read-only — see F3** |
| `rankings_daily` | 175 | `daily_rankings` → `snapshot_rankings` | `daily_rankings` | OK |
| `odds_snapshots` | 181,982 | `snapshot_odds` | — (consumed by build/scripts) | write-only on the app path |
| `closing_lines` | 7,184 | `snapshot_closing` | — | write-only on the app path |
| `weather_snapshots` | 40,782 | `snapshot_weather` | — | write-only on the app path |
| `injury_snapshots` | 402 | `snapshot_injuries` | — | write-only on the app path |
| `model_predictions` | 272 | `snapshot_predictions` | — | write-only (backtest consumer) |
| `raw_payloads` | 6 | `snapshot_raw_payload` | — | write-only, near-empty |
| `served_snapshots` | 128 | `snapshot_served_state` | `snapshot_served_state` | OK (change detection) |
| `freshness_events` | 201 | `record_freshness_event` | — (ops/scripts read it) | OK |
| `games` | 21,204 | **NONE** | — | **backfill-only — see F1** |
| `backtest_runs` | 12 | script-only (`backtest_rig.py`) | script-only | OK (offline) |
| `players` | **0** | **NONE** | **NONE** | **orphan — see F3** |

---

## 4. The disk tier — the same bug class, still live

`app.py` reads at least these from the container's **ephemeral** disk. Any of these that
is *served* (rather than merely cached) reverts to its build-time copy on every recycle,
exactly like `cfbd_analytics.json` did.

| file | read at serve/build time by | risk |
|---|---|---|
| `data/week_schedule.json` | `load_schedule()` ← `api_schedule()` | **served** — F2 |
| `data/odds_cache.json` | odds path | served |
| `data/line_history.json` / `line_movements.json` | line-movement path | served |
| `data/best_line_store.json` / `best_line_ts.json` | line source of record | served |
| `data/record.json` | grading / record display | served |
| `data/finals_cache.json` | final scores | served |
| `data/best_bets.json` | picks display | served |
| `data/active_injuries.json` | injury flags | served |
| `data/cfbd_logos.json`, `data/fbs_teams.json` | static reference | low (static) |
| `data/cfbd_analytics.json` | fallback under `_served_analytics()` | low since v51 (D1 first) |

**Note:** several of these ARE re-fetched on boot (odds, lines), which is why they have
not produced a visible incident yet. But "re-fetched on boot" is the same unverified
assumption that hid the analytics bug for a week. Each needs the same D1-first treatment
or an explicit statement of why disk is safe.

---

## 5. Findings, ranked

### Dispositions (Phase 7, 2026-09-28)

| Finding | Disposition | Receipt |
|---|---|---|
| F1 — results served from an ephemeral cache | **RESOLVED** (v52, Phase 2) | D1 `games` is the door (`snapshot_games`/`load_games`); `test_live_finals_fetch_archives_to_d1` proves the live fetch archives |
| F2 — ephemeral read-modify-write loses writes | **RESOLVED** (v53 + Phase 6) | `week_schedule`, `active_injuries`, `best_bets`, `line_movements`, `odds_cache` — each D1-first with a durability test |
| F3 — `teams` read-only; `players` orphan | **RESOLVED** (v60) | `snapshot_team_identity()` live writer; served universe covers D1; `players` KEEP/unwired per Jeff |
| F4 — no pull history in `stat_observations` | **RESOLVED** (v62, Phase 4) | unique index now includes `recorded_at`; writer adapts to the live index; `tests/test_stat_obs_append_only.py` proved n==1 before / n==2 after |
| F5 — `@_guard` swallows failures | **RESOLVED** (v54, Phase 5) | `freshness_events` + `/api/health.archive`; `tests/test_archive_failures_surface.py` |
| F6 — D1 reads gated on the write flag | **RESOLVED** (v52) | `read_enabled()` split from `write_enabled()`; flag contract test |
| F7 — nothing asserts served == D1 | **RESOLVED** | the parity suite is a blocking step of the deploy gate |
| F8 — 8 tables written with no live reader | **ACCEPTED (INFO)** — per-table intent, not a code change; several are deliberately offline (backtests, ops). Revisit only if one is *expected* to feed the site | — |

### F1 — Results/games are not persisted live. **HIGH.**
D1 `games` has **0 scored games for 2026 week ≥ 5**; the newest scored rows are the
9/21 backfill. The live app *grades* results (`_ingest_results`) into `data/record.json`
on **disk**, and reads final scores live from CFBD each time. `upsert_games` is called
only by `scripts/backfill_d1.py`.

*Consequence:* D1's `games` table is a frozen 9/21 snapshot. Any D1-based analysis
(backtests, joins, "what happened last week") silently excludes everything since —
with no error. This is the same failure shape as v50.

### F2 — The schedule board is served partly from disk. **HIGH.**
`load_schedule()` opens `data/week_schedule.json` and is called by `api_schedule()`
(app.py:4429, 4588). On the container that file is ephemeral. `/api/schedule` also reads
the durable `slate_cache`, so there are **two sources for one page** — precisely the
condition that made the analytics bug invisible.

### F3 — **CLOSED 2026-09-28 (v60).** `teams` has a live writer now; `players` stays KEEP/unwired.

Was: `teams` read by the live path, written only by `scripts/backfill_d1.py`. Now
`_store_team_identity()` rides every analytics archive, and `_served_analytics()` serves D1
identity rows so nothing in `teams` is unrenderable. Measured: 3 of the 5 apparent mismatches
were the aliases `cfbd_shared.team_aliases()` already documents; the real drift was Anna Maria
College and Defiance College (D-III, zero analytics rows). `players` remains as Jeff decided —
KEEP, reserved for player-vs-player matchups, not wired to anything.
`teams` is read by the live path but written by **nothing** on it (`upsert_teams` exists
only in backfill scripts/selftests) — so new teams/conference changes require a manual
backfill. `players` has **0 rows, no writer, no reader** — it was created and never used.

### F4 — No pull history in `stat_observations`. **MEDIUM (auditability, not loss).**
The unique key `(subject_type, subject_id, season, stat_key, week)` is an upsert, so the
four weekly anchors collapse to one row per metric. **If the values are identical this
costs nothing** — the earlier alarm overstated it. It costs something only when CFBD
revises mid-window, and it means you cannot tell from D1 whether a given pull archived.

### F5 — `@_guard` swallows archive failures. **MEDIUM.**
Every `snapshot_*` in `d1_write_path.py` catches all exceptions, returns `0` and prints
"failed (site unaffected)". A dead archive looks healthy. Nothing alerts.

### F6 — D1 reads are gated on `D1_WRITE_ENABLED`. **LOW/MEDIUM.**
`d1_write_path.enabled()` is checked by *readers* too, so with the write flag off, reads
silently return `[]` and callers fall back to disk without warning. (This is what made my
first counterfactual test report a false FAIL.)

### F7 — Nothing asserts served == D1. **MEDIUM.**
The two audit scripts now exist but are not wired to the deploy or to a schedule, so
drift is still only found by hand. A parity check belongs in `cfb_deploy.sh`'s verify
step and/or the Wednesday smoke.

### F8 — 8 tables are written with no live reader. **INFO.**
`odds_snapshots`, `closing_lines`, `weather_snapshots`, `injury_snapshots`,
`model_predictions`, `raw_payloads`, plus `games`/`players` above. Some are legitimately
offline-only (backtests, ops). But if any is *expected* to feed the site, the consumer is
missing — same shape as the v49 bug. Needs a per-table intent decision, not a code change.

### F9 — Board/schedule disk fallbacks are read with no verified publication. **LOW — by design, now tracked.**
`cfbd_analytics.json` (serve **and** build, via `compute_win_totals()`) and `week_schedule.json`
(build, via `load_schedule()`) are still read from `data/` when the store holds no verified
publication. That is the documented disk fallback: it keeps a board or a page rendering during a
D1/provider outage instead of serving an empty result.
`tests/test_no_disk_reads_in_serving.py` allowlists exactly these three reads under this id, and
still fails if any *other* data/ read appears.
**Removal:** Phase 6 routes both through the D1-first accessors (`_served_analytics()`); delete the
allowlist entries with that conversion so the test goes red if either read comes back.

---

## 6. Recommended order

1. **F1** — persist results into D1 `games` on the live path (or state explicitly that D1
   games is a backfill artifact and nothing may read it for recency).
2. **F2** — make the schedule serve D1-first, single source, like `/api/analytics` now does.
3. **F7** — wire a served-vs-D1 parity check into the deploy verify step.
4. **F5/F6** — make archive failures visible; split the read flag from the write flag.
5. **F3** — decide the `teams` write path and delete or use `players`.
6. **F4** — decide on append-only history (row growth ~2×/day) — Jeff's call.
