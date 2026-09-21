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
