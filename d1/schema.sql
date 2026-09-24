-- Skeez CFB persistent data — D1 schema (Phase 2)
-- Database: cfb-history   Spec: D1_SCHEMA_SPEC.md
-- Principle: append-only history; long-format observations so new stats need no migration.

-- 1. Identity layer -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS teams (
  team_id      INTEGER PRIMARY KEY,   -- CFBD numeric team id (stable join key)
  name         TEXT,
  abbr         TEXT,
  conference   TEXT,
  first_season INTEGER,
  classification TEXT            -- fbs | fcs | ii | iii  (CFBD /teams; FCS slicing)
);

CREATE TABLE IF NOT EXISTS players (
  player_id INTEGER PRIMARY KEY,
  team_id   INTEGER,
  name      TEXT,
  position  TEXT
);

-- 2. Core: long-format observations ------------------------------------------
CREATE TABLE IF NOT EXISTS stat_observations (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  subject_type TEXT,      -- 'team' | 'player'
  subject_id   INTEGER,   -- team_id | player_id
  season       INTEGER,
  week         INTEGER,   -- 0 = preseason/pre-week
  stat_key     TEXT,
  value        REAL,
  source       TEXT,      -- 'cfbd' | 'computed' | 'manual'
  recorded_at  TEXT
);
-- Dedupe guard: one value per (subject, season, week, key). Enables idempotent upsert.
CREATE UNIQUE INDEX IF NOT EXISTS ux_stat_obs_subject
  ON stat_observations(subject_type, subject_id, season, stat_key, week);
-- Backtest query: "all teams, one stat, one week".
CREATE INDEX IF NOT EXISTS ix_stat_obs_season_key_week
  ON stat_observations(season, stat_key, week);

CREATE TABLE IF NOT EXISTS games (
  game_id    INTEGER PRIMARY KEY,  -- CFBD game id
  season     INTEGER,
  week       INTEGER,
  home_id    INTEGER,
  away_id    INTEGER,
  kickoff    TEXT,
  home_score INTEGER,              -- NULL until final
  away_score INTEGER,
  status     TEXT,                 -- scheduled | final | canceled
  venue      TEXT,
  neutrality INTEGER
);
CREATE INDEX IF NOT EXISTS ix_games_season_week ON games(season, week);
CREATE INDEX IF NOT EXISTS ix_games_home        ON games(home_id);
CREATE INDEX IF NOT EXISTS ix_games_away        ON games(away_id);

CREATE TABLE IF NOT EXISTS closing_lines (
  game_id        INTEGER,
  book           TEXT,             -- 'consensus' | 'cfbd' | book name
  spread_home    REAL,             -- home-favored negative
  total          REAL,
  home_moneyline INTEGER,
  away_moneyline INTEGER,
  captured_at    TEXT
);
CREATE INDEX IF NOT EXISTS ix_closing_lines_game ON closing_lines(game_id);

-- 3. Live append-only streams -------------------------------------------------
CREATE TABLE IF NOT EXISTS odds_snapshots (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  game_id     INTEGER,
  book        TEXT,
  spread_home REAL,
  total       REAL,
  home_ml     INTEGER,
  away_ml     INTEGER,
  poll_ts     TEXT                 -- all rows in one poll share it
);
CREATE INDEX IF NOT EXISTS ix_odds_snap_game_ts ON odds_snapshots(game_id, poll_ts);

CREATE TABLE IF NOT EXISTS rankings_daily (
  team_id       INTEGER,
  date          TEXT,
  composite     REAL,
  rank          INTEGER,
  model_version TEXT,              -- hash of the composite config
  season        INTEGER,
  week          INTEGER
);
CREATE INDEX IF NOT EXISTS ix_rank_daily_date     ON rankings_daily(date);
CREATE INDEX IF NOT EXISTS ix_rank_daily_team_date ON rankings_daily(team_id, date);

CREATE TABLE IF NOT EXISTS model_predictions (
  game_id               INTEGER,
  model_version         TEXT,
  predicted_margin_home REAL,
  predicted_total       REAL,
  win_prob_home         REAL,
  created_at            TEXT,      -- written BEFORE kickoff (no hindsight)
  -- Weather at write time (Part 4 of the 2026-09-22 work order). CFBD /games/weather
  -- is fetched per-request and is NOT a historical feed, so if these rows do not carry
  -- it, that game's weather is gone forever. Populated from the same pre-kickoff write
  -- that creates the row, so the value is what the model actually saw.
  wind_mph              REAL,
  temp_f                REAL,
  condition             TEXT,
  indoor                INTEGER,
  wind_penalty          REAL
);

-- ------------------------------------------------------- Injury snapshots (Part 2)
-- Our OWN historical injury feed. No external source publishes a point-in-time CFB
-- injury list, so a backtest of the star-QB rule can only ever exist if we start
-- recording what we knew BEFORE each kickoff. This table is that record.
--
-- ONE ROW PER TEAM PER GAME, written pre-kickoff (same no-hindsight rule as
-- model_predictions — a row whose kickoff has passed is refused at the write path).
-- `injury_list` is the JSON the model actually consumed (player, position, severity,
-- star_level); `injury_adj_applied` is the points value it applied. `actual_*` and
-- `residual_*` are NULL at write time BY DESIGN and are filled by a later
-- reconciliation pass once the game is final — that is what makes the row a
-- self-contained experiment instead of a note.
CREATE TABLE IF NOT EXISTS injury_snapshots (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  game_id            INTEGER,
  season             INTEGER,
  week               INTEGER,
  team               TEXT,
  opponent           TEXT,
  kickoff_ts         TEXT,
  injury_list        TEXT,     -- JSON array: [{player, position, severity, star_level}]
  injury_adj_applied REAL,     -- points applied to the margin (negative = worse)
  predicted_margin   REAL,     -- model margin for THIS team (positive = this team favored)
  predicted_total    REAL,
  actual_margin      REAL,     -- NULL pre-kickoff
  actual_total       REAL,     -- NULL pre-kickoff
  residual_margin    REAL,     -- actual - predicted, filled post-game
  residual_total     REAL,
  model_version      TEXT,
  created_at         TEXT,
  settled_at         TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_injury_snap_team_game
  ON injury_snapshots(game_id, team, model_version);
CREATE INDEX IF NOT EXISTS ix_injury_snap_season ON injury_snapshots(season, week);
CREATE INDEX IF NOT EXISTS ix_injury_snap_game   ON injury_snapshots(game_id);

CREATE TABLE IF NOT EXISTS raw_payloads (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  endpoint   TEXT,                 -- e.g. 'cfbd/games?year=2022'
  fetched_at TEXT,
  payload_gz BLOB                  -- zlib-compressed JSON
);

-- slate_cache — the finished Schedule payload for a (season, week), so the page is a
-- READ instead of a recompute. Rebuilt when the composite inputs actually change
-- (change-triggered, not per-request): the inputs only move Sun-Wed, so Thursday-
-- Saturday every request was re-deriving a number that could not have changed.
-- `fingerprint` is a hash of the enabled composite inputs; identical inputs leave the
-- stored row alone. payload_gz is gzip+b64 (same convention as raw_payloads).
CREATE TABLE IF NOT EXISTS slate_cache (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  season        INTEGER,
  week          INTEGER,
  built_at      TEXT,
  fingerprint   TEXT,
  model_version TEXT,
  payload_gz    BLOB
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_slate_cache_season_week
  ON slate_cache (season, week);

-- weather_snapshots — our OWN weather history, one row per game per poll.
--
-- CFBD's /games/weather is a live lookup with NO historical endpoint, so a game's
-- conditions are unrecoverable the moment the season ends. Capturing them as a
-- side effect of model_predictions covered only the games that got a prediction
-- row (~213 of a ~1,656-game slate); this stream covers the whole slate.
-- Written pre-kickoff only (same D2 no-hindsight rule as the other streams): a
-- post-game reading is not a forecast and must not masquerade as one.
-- Starts empty by design; CFBD offers no backfill for it.
CREATE TABLE IF NOT EXISTS weather_snapshots (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  game_id     INTEGER,
  season      INTEGER,
  week        INTEGER,
  kickoff_utc TEXT,
  poll_ts     TEXT,                -- all rows in one poll share it
  wind_mph    REAL,
  temp_f      REAL,
  condition   TEXT,
  indoor      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_weather_snap_game_poll
  ON weather_snapshots (game_id, poll_ts);
CREATE INDEX IF NOT EXISTS idx_weather_snap_season_week
  ON weather_snapshots (season, week);

-- ---------------------------------------------------------------- Phase 3.5
-- Standing budget meters (D1_CHECKLIST Phase 3.5). Ledger of RECORD for API
-- burn: the container filesystem is ephemeral, so a file-only ledger resets on
-- every instance recycle. Written sparsely (one row per source per bucket per
-- flush, ~600s) so its own quota cost is negligible.
CREATE TABLE IF NOT EXISTS api_usage (
  bucket             TEXT,         -- 'day' | 'month'
  period             TEXT,         -- 'YYYY-MM-DD' | 'YYYY-MM'
  source             TEXT,         -- cfbd | odds | propline | d1
  calls              INTEGER,      -- provider-reported usage when known, else our count
  provider_remaining INTEGER,      -- authoritative header values (NULL if unseen)
  provider_limit     INTEGER,
  provider_used      INTEGER,
  updated_at         TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_api_usage ON api_usage(bucket, period, source);

-- ------------------------------------------------------------- Freshness telemetry
-- Answers the questions nobody could answer on 2026-09-22 after ~39.5h of stale
-- analytics: WHEN did the container last start, WHEN did a pull last succeed or
-- fail, and WHY (which trigger). Before this table the only evidence was a
-- console.log that Cloudflare does not retain, so the incident was undiagnosable
-- after the fact and QA could only return INCONCLUSIVE for the freshness SLA and
-- the cold-start self-heal.
--
-- Append-only and low-volume (a handful of rows per container start plus one per
-- pull attempt), so its own quota cost is negligible. Written through the guarded
-- D1 write-path: a telemetry failure must never affect the site.
CREATE TABLE IF NOT EXISTS freshness_events (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_utc    TEXT,      -- ISO8601 UTC, when the event occurred
  event     TEXT,      -- container_start | pull_success | pull_skip | pull_failed
  source    TEXT,      -- scheduler | cron | guard | manual  (what triggered it)
  age_hours REAL,      -- age of the last successful pull at event time (NULL if unknown)
  build_tag TEXT,      -- deployed image tag — makes an image swap visible in the timeline
  detail    TEXT       -- free text or JSON (status, error, counts)
);
CREATE INDEX IF NOT EXISTS ix_freshness_ts    ON freshness_events(ts_utc);
CREATE INDEX IF NOT EXISTS ix_freshness_event ON freshness_events(event, ts_utc);

-- ------------------------------------------------------------- Served-state snapshots
-- QA could not establish the user-visible BLAST RADIUS of the 2026-09-22 staleness
-- incident: freshness_events proves how long the data was stale, but nothing
-- retained WHAT the site actually served during that window, so "which rankings /
-- win-totals / schedule values did users see" was unanswerable and stayed
-- INCONCLUSIVE forever.
--
-- This table closes that going forward. Once per day the app summarises what it is
-- currently serving — per endpoint: the data's own `as_of` timestamp, a row count,
-- and a content hash — so a past window can be resolved exactly instead of guessed.
-- Deliberately a SUMMARY (no full payloads): a few rows a day, negligible quota, and
-- no new privacy surface.
CREATE TABLE IF NOT EXISTS served_snapshots (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  date         TEXT,     -- 'YYYY-MM-DD' (UTC) — one snapshot set per day
  ts_utc       TEXT,     -- when the snapshot was taken
  endpoint     TEXT,     -- /api/rankings | /api/analytics | /api/win-totals | /api/schedule
  as_of        TEXT,     -- timestamp of the UNDERLYING DATA (not of the request)
  row_count    INTEGER,  -- teams / games / rows served
  content_hash TEXT,     -- sha256[:32] of the canonical payload, to detect drift
  build_tag    TEXT,     -- deployed image tag
  detail       TEXT      -- free text or JSON
);
CREATE INDEX IF NOT EXISTS ix_served_snap_date ON served_snapshots(date);
CREATE INDEX IF NOT EXISTS ix_served_snap_ep   ON served_snapshots(endpoint, date);

-- ------------------------------------------------------------- Durable app state
-- Small key/value store for state that must SURVIVE a container recycle.
--
-- Motivated by a real bug found 2026-09-22: the "last analytics pull" marker was a
-- file (data/last_analytics_pull.json). Two things were wrong with that:
--   1. the container filesystem is ephemeral, so the app could never durably update
--      it -- every recycle lost the value;
--   2. Dockerfile `COPY data/ ./data/` copies the WORKING TREE (including untracked
--      files), so a stale local copy from Sep 20 was baked into every image. The
--      app therefore reported the same ~41h-old "last pull" no matter how fresh the
--      data actually was, and every fresh container believed it was due.
-- (2) was accidentally load-bearing: that eager pull is what masked the missed
-- anchor. It is now replaced by an explicit, age-based staleness guard.
CREATE TABLE IF NOT EXISTS app_state (
  key        TEXT PRIMARY KEY,
  value      TEXT,
  updated_at TEXT
);

-- ---------------------------------------------------------------------------
-- backtest_runs — one row per ARM per experiment run.
--
-- Why this exists: the backtest harness could always produce a number, but there was
-- nowhere for the number to LIVE. Every experiment was ephemeral — a JSON file written
-- by hand, a chat message, a memory. That makes "did this weighting help?" unanswerable
-- over time, because yesterday's result cannot be re-read in context.
--
-- model_version is the AUDIT KEY: it is the hash of the exact weight set used, so an arm
-- can never be silently compared against a different one (same guarantee the
-- rankings_daily / model_predictions rows rely on). Weights themselves are stored as
-- JSON so the row is self-describing even if the arms file is later edited.
--
-- scope records WHICH games were evaluated (e.g. fbs_only_tiers_vs_all_games), because
-- a win rate without its scope is not comparable to anything.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS backtest_runs (
  run_id          TEXT NOT NULL,   -- experiment id (UTC timestamp), groups the arms
  ts_utc          TEXT NOT NULL,
  arm             TEXT NOT NULL,   -- 'baseline', 'zero-srs', ...
  model_version   TEXT,            -- composite hash for that arm's weights
  weights_json    TEXT,            -- the exact weights used, self-describing
  season          INTEGER,
  games           INTEGER,         -- total evaluated games
  fbs_matchups    INTEGER,         -- games counted in the ATS/totals tiers
  ats_all         REAL,            -- win pct, all FBS spread picks
  ats_3star       REAL,
  ats_5star       REAL,
  totals_all      REAL,
  totals_3star    REAL,
  dog_share_pct   REAL,            -- how often the model took the underdog
  mean_model_margin REAL,          -- mean |model spread| (compression check)
  mean_book_line  REAL,
  mae_vs_book     REAL,
  bias_vs_book    REAL,
  note            TEXT,
  PRIMARY KEY (run_id, arm)
);

CREATE INDEX IF NOT EXISTS idx_backtest_runs_ts ON backtest_runs(ts_utc DESC);
