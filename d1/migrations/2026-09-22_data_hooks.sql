-- Migration 2026-09-22 — Part 2 + Part 4 of the V6.1 work order.
--
-- WHY A SEPARATE FILE: d1/schema.sql uses CREATE TABLE IF NOT EXISTS, which is a no-op
-- against the ALREADY-LIVE cfb-history database — so adding the weather columns to that
-- CREATE only helps a fresh database. The live table needs explicit ALTERs, and each of
-- these must run exactly ONCE (SQLite has no ADD COLUMN IF NOT EXISTS; re-running errors).
--
-- Apply with:
--   npx wrangler d1 execute cfb-history --remote --file=d1/migrations/2026-09-22_data_hooks.sql
--
-- Additive only: no column is dropped, renamed or retyped, and no existing row is touched.
-- Safe to apply before the code that reads/writes these columns is deployed.

ALTER TABLE model_predictions ADD COLUMN wind_mph REAL;
ALTER TABLE model_predictions ADD COLUMN temp_f REAL;
ALTER TABLE model_predictions ADD COLUMN condition TEXT;
ALTER TABLE model_predictions ADD COLUMN indoor INTEGER;
ALTER TABLE model_predictions ADD COLUMN wind_penalty REAL;

-- Part 2: our own point-in-time injury feed (see d1/schema.sql for the field notes).
CREATE TABLE IF NOT EXISTS injury_snapshots (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  game_id            INTEGER,
  season             INTEGER,
  week               INTEGER,
  team               TEXT,
  opponent           TEXT,
  kickoff_ts         TEXT,
  injury_list        TEXT,
  injury_adj_applied REAL,
  predicted_margin   REAL,
  predicted_total    REAL,
  actual_margin      REAL,
  actual_total       REAL,
  residual_margin    REAL,
  residual_total     REAL,
  model_version      TEXT,
  created_at         TEXT,
  settled_at         TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_injury_snap_team_game
  ON injury_snapshots(game_id, team, model_version);
CREATE INDEX IF NOT EXISTS ix_injury_snap_season ON injury_snapshots(season, week);
CREATE INDEX IF NOT EXISTS ix_injury_snap_game   ON injury_snapshots(game_id);