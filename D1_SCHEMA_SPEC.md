# D1 SCHEMA SPEC — Skeez CFB persistent data (Phase 2)

**Status:** DRAFT for Jeff's review — not a work order until approved.
**Target:** Cloudflare D1 (SQLite), database `cfb-history`. Free tier: 5 GB storage, 5M row-reads/day, 100K row-writes/day.
**Principle:** add stats without migrations (long-format observations), never lose history (append-only), everything queryable by SQL for backtests.

---

## 1. Identity layer

### `teams`
Stable team registry. CFBD's numeric team id is the join key — it never changes.

| column | type | notes |
|---|---|---|
| team_id | INTEGER PK | CFBD team id |
| name | TEXT | current display name |
| abbr | TEXT | |
| conference | TEXT | current conference |
| first_season | INTEGER | for backfill coverage checks |

Conference changes over time (realignment) are *observations*, not edits — see `stat_observations`.

### `players`
| column | type | notes |
|---|---|---|
| player_id | INTEGER PK | CFBD player id |
| team_id | INTEGER | current team (roster changes are observations, not edits) |
| name | TEXT | |
| position | TEXT | |

## 2. The core: long-format observations

### `stat_observations`
One row = one numeric value for one subject at one point in time. **This is the table that makes new stats free.**

| column | type | notes |
|---|---|---|
| id | INTEGER PK | |
| subject_type | TEXT | 'team' or 'player' |
| subject_id | INTEGER | team_id or player_id |
| season | INTEGER | |
| week | INTEGER | 0 = preseason/pre-week |
| stat_key | TEXT | vocabulary below |
| value | REAL | numeric only |
| source | TEXT | 'cfbd' / 'computed' / 'manual' |
| recorded_at | TEXT | when WE stored it |

**Indexes:** `(subject_type, subject_id, season, stat_key, week)` unique-ish (dedupe guard),
`(season, stat_key, week)` for "all teams, one week" backtest queries.

**Stat-key vocabulary (initial seed — extend freely, no migration):**
`elo`, `sp_plus_offense`, `sp_plus_defense`, `sp_plus_rk`, `fpi`, `recruiting_class_rank`,
`recruiting_5star_count`, `recruiting_4star_count`, `talent_rating`, `returning_production`,
`ppa_offense_avg`, `ppa_defense_avg`, `havoc_rate`, `injuries_count`, `coach_tenure`,
`win_streak`, `rest_days`, `distance_traveled`, plus every per-game team stat CFBD exposes
(`total_yards`, `pass_yards`, `rush_yards`, `turnovers`, `penalties`, ...).

**Non-numeric facts** (conference membership, coach name, head-coach changes) live in
`raw_payloads` (below), not here. Rule: if it can't be a number, it's a payload or a text
column on a dedicated table — no string values in the numeric observations table.

### `games`
Canonical schedule + scores (backfill + live).

| column | type | notes |
|---|---|---|
| game_id | INTEGER PK | CFBD game id |
| season, week | INTEGER | |
| home_id, away_id | INTEGER | team ids |
| kickoff | TEXT | |
| home_score, away_score | INTEGER | NULL until final |
| status | TEXT | scheduled/final/canceled |
| venue, neutrality | TEXT/INTEGER | |

**Index:** `(season, week)`, `(home_id)`, `(away_id)`.

### `closing_lines`
One row per game per line type — the backtest ground truth. Populated from CFBD's
historical `lines` endpoint (backfill) and from live polls when a line closes.

| column | type | notes |
|---|---|---|
| game_id | INTEGER | |
| book | TEXT | 'consensus', 'cfbd', book names |
| spread_home | REAL | home-favored negative |
| total | REAL | |
| home_moneyline, away_moneyline | INTEGER | optional |
| captured_at | TEXT | |

**Index:** `(game_id)`.

## 3. Live append-only streams

### `odds_snapshots`
Every hourly poll, every game, every book. Append-only, never edited.

| column | type | notes |
|---|---|---|
| id | INTEGER PK | |
| game_id | INTEGER | |
| book | TEXT | |
| spread_home, total | REAL | |
| home_ml, away_ml | INTEGER | |
| poll_ts | TEXT | poll timestamp (all rows in one poll share it) |

**Volume reality:** ~50 games × ~16 polls/day ≈ 800 rows/day ≈ 290K rows/season —
well within free tier. **Write-cap reality:** 100K row-writes/day on free tier; a normal
day (800 rows) uses <1%. The 5-year backfill does NOT touch this table (see closing_lines).

**Index:** `(game_id, poll_ts)`.

### `rankings_daily`
One row per team per day — the "what did the model say" archive.

| column | type | notes |
|---|---|---|
| team_id | INTEGER | |
| date | TEXT | |
| composite | REAL | |
| rank | INTEGER | |
| model_version | TEXT | composite config hash — weight changes are versioned, comparable |
| season, week | INTEGER | |

**Index:** `(date)`, `(team_id, date)`.

### `model_predictions`
Per-game model output, stored *before* the game is played (no hindsight bias).

| column | type | notes |
|---|---|---|
| game_id | INTEGER | |
| model_version | TEXT | |
| predicted_margin_home | REAL | |
| predicted_total | REAL | |
| win_prob_home | REAL | |
| created_at | TEXT | |

### `raw_payloads`
Compressed (zlib) JSON of every CFBD/API response used for backfill or major refreshes.
Insurance: if the API's numbers or definitions change later, we can re-derive without
re-pulling. Lowest-priority table; prunable by age if storage ever matters.

| column | type | notes |
|---|---|---|
| id | INTEGER PK | |
| endpoint | TEXT | e.g. 'cfbd/games?year=2022' |
| fetched_at | TEXT | |
| payload_gz | BLOB | |

### `weather_snapshots`
Our OWN weather history: one row per game per poll, written hourly, pre-kickoff only.

CFBD's `/games/weather` is a live lookup with **no historical endpoint** — a game's
conditions are unrecoverable the moment the season ends. Before this table, weather
existed only as a by-product of `model_predictions`, so coverage was the games that
happened to get a prediction row (~213 rows against a slate of ~1,656 games). This
stream covers every game CFBD returns a reading for (measured: 237 rows for week 4
on the first poll).

It starts **empty by design** and only accumulates; there is nothing to backfill from
CFBD. Values come from the same parser the model consumes (`_cfbd_weather` via
`d1_write_path._wx`), so this table can never disagree with what the model saw.
Hourly to match the odds poll, so a weather reading and a line reading carry the same
`poll_ts` and can be joined for "what did we know at decision time".

| column | type | notes |
|---|---|---|
| id | INTEGER PK | |
| game_id | INTEGER | CFBD game id |
| season | INTEGER | |
| week | INTEGER | |
| kickoff_utc | TEXT | from the game's startDate |
| poll_ts | TEXT | all rows in one poll share it |
| wind_mph | REAL | |
| temp_f | REAL | |
| condition | TEXT | |
| indoor | INTEGER | 0/1 |

Keyed `(game_id, poll_ts)`: a retry inside the same poll replaces rather than
duplicates; the next poll's rows are new rows on purpose — the series is the point.
Backtest access pattern: `idx_weather_snap_season_week`.

---

## 4. Backfill plan (5 seasons: 2021–2025, plus current 2026 live)

| Source | Tables filled | Est. rows | API requests (paced) |
|---|---|---|---|
| CFBD games | games | ~7,000 | ~700 (season pages) |
| CFBD team stats (season+week) | stat_observations | ~150K | ~1,500 |
| CFBD lines | closing_lines | ~7,000 | ~350 |
| CFBD SP+/Elo/talent/recruiting | stat_observations | ~30K | ~400 |
| **Player stats — DEFERRED** | stat_observations (subject_type='player') | millions | **thousands** |

**Hard truth on player stats:** the volume is 10–50× team stats and will blow the
100K row-writes/day free cap for days and possibly pressure the 5 GB storage limit.
Decision needed (below). Team-level backfill is complete without players and unlocks
all the composite-weight backtesting — players are additive depth, not a prerequisite.

**Pacing requirements for the backfill script (CTO work order):**
- Throttle to stay under CFBD rate limits; respect 429s with backoff.
- **Checkpointing:** persist progress (season/endpoint/last-page) after every chunk —
  a crashed backfill resumes, never restarts.
- Batched inserts (D1 accepts multi-statement batches) to conserve request budget.
- Idempotent: re-running a chunk must not duplicate rows (the dedupe indexes enforce it).

## 5. Write-path (live) — app changes

1. `d1_store.py` module in the app: thin wrapper over D1 REST API using the existing
   Cloudflare API token (already provisioned, DNS-edit verified).
2. On every hourly refresh: after pulling CFBD/odds data, append `odds_snapshots` rows
   (single batched request) and upsert changed `stat_observations`.
3. Nightly (piggyback on the 9PM PT refresh): write `rankings_daily` rows for all teams.
4. On game final: write `closing_lines` (from the live poll that observed the close) and
   `model_predictions` before kickoff — the predictions table is written pre-game by design.
5. Retention: everything kept forever. If storage ever approaches 5 GB, `raw_payloads`
   is pruned first (oldest first) — it's the only disposable table.

## 6. Backtest interface (what this buys)

A `backtest.py` pattern against this schema, no new tables:

```sql
-- "How predictive was Elo, by season?"
SELECT s.season,
       corr(s.value, CASE WHEN g.home_score > g.away_score THEN 1 ELSE 0 END) ...
FROM stat_observations s JOIN games g ...
WHERE s.stat_key = 'elo' GROUP BY s.season;
```

Weight optimization: export any season's stat matrix + outcomes as a frame, test weight
variants against held-out weeks. The `model_version` column on `rankings_daily` /
`model_predictions` keeps every historical config comparable.

## 7. DECIDED (Jeff, Sep 20) — no longer open

1. **Player stats: DEFERRED.** Team-level backfill only. Players added later as a paced
   extension (100K rows/day cap respected).
2. **Backfill scope: 2021–2025 full seasons + 2026 weeks 1–3** (current season to date —
   there is no persistent data from 2026; the live season gets captured going forward).
3. **Model version: automatic** — TEXT hash of the composite config. Human labels not
   required (super-bots read the hash; Jeff doesn't need to).

## 8. Non-goals (explicit)

- No read endpoint on the public site in Phase 2 — D1 is internal memory. Public history
  pages are a later feature reading the same tables.
- No player-level predictions in Phase 2.
- No deletion policy: everything forever (5 GB is years of headroom; `raw_payloads` is the
  designated pressure valve).
