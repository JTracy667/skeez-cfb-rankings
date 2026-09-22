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
  created_at            TEXT       -- written BEFORE kickoff (no hindsight)
);

CREATE TABLE IF NOT EXISTS raw_payloads (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  endpoint   TEXT,                 -- e.g. 'cfbd/games?year=2022'
  fetched_at TEXT,
  payload_gz BLOB                  -- zlib-compressed JSON
);

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
