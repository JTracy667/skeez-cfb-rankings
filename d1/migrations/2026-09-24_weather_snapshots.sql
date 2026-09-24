-- Migration 2026-09-24 — weather as a first-class append-only stream (Jeff's call).
--
-- WHY: weather was only captured as a SIDE EFFECT of writing model_predictions, so it
-- existed on 213 rows out of a slate that runs ~1,656 games. CFBD's /games/weather is a
-- live lookup with NO history — once a season is over the conditions are gone forever.
-- A dedicated stream means the coverage is the whole slate, every poll, and the backtest
-- dataset grows on its own from here forward.
--
-- CfBD has no historical weather, so this table starts empty and only accumulates.
-- (A third-party reanalysis API could backfill 2021-25 later; that is a separate,
-- Research-owned decision — parked by Jeff 2026-09-24.)
--
-- WHY A SEPARATE FILE: d1/schema.sql uses CREATE TABLE IF NOT EXISTS, which is a no-op
-- against the ALREADY-LIVE cfb-history database, so it only helps a fresh database.
--
-- Apply with:
--   npx wrangler d1 execute cfb-history --remote --file=d1/migrations/2026-09-24_weather_snapshots.sql
--
-- Additive only: creates a new table + indexes. Touches no existing table or row.

CREATE TABLE IF NOT EXISTS weather_snapshots (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  game_id     INTEGER,
  season      INTEGER,
  week        INTEGER,
  kickoff_utc TEXT,
  poll_ts     TEXT,
  wind_mph    REAL,
  temp_f      REAL,
  condition   TEXT,
  indoor      INTEGER
);

-- (game_id, poll_ts) is the natural key: one reading per game per poll. The writer
-- deletes-then-inserts on it, so a retry inside the same poll cannot duplicate rows.
CREATE INDEX IF NOT EXISTS idx_weather_snap_game_poll
  ON weather_snapshots (game_id, poll_ts);

-- Backtest access pattern: "every weather reading for week N of season S, as it
-- stood before each kickoff".
CREATE INDEX IF NOT EXISTS idx_weather_snap_season_week
  ON weather_snapshots (season, week);