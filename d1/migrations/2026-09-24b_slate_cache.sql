-- Migration 2026-09-24b — slate_cache: serve the Schedule page from a stored payload.
--
-- WHY: every Schedule load recomputed all ~71 games from live sources. Measured on
-- prod: the first load after a container sleeps cost 9.0s (12.6s before the odds fix),
-- while repeats were 0.29-0.52s. Containers recycle on 5m idle and the site is
-- low-traffic, so the FIRST visitor is the normal case and pays the cold cost.
--
-- The composite inputs only change Sun-Wed (CFBD publishes SP+/efficiency/returning
-- production then; talent only decays by week number), so recomputing per request is
-- pure waste Thursday-Saturday. This table holds the finished payload; the request
-- path reads it, and it is REBUILT whenever the inputs actually change -- change-
-- triggered, not clock-triggered.
--
-- `fingerprint` is a hash of the enabled composite inputs for every team. If a refresh
-- produces the same inputs, the stored copy is left alone; only a real value change
-- causes a rewrite.
--
-- Apply with:
--   npx wrangler d1 execute cfb-history --remote --file=d1/migrations/2026-09-24b_slate_cache.sql
--
-- Additive only: new table + index.

CREATE TABLE IF NOT EXISTS slate_cache (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  season      INTEGER,
  week        INTEGER,
  built_at    TEXT,
  fingerprint TEXT,
  model_version TEXT,
  payload_gz  BLOB
);

-- One current copy per (season, week): the writer deletes the old row on the same key,
-- so readers never see two candidates and the table cannot grow per poll.
CREATE UNIQUE INDEX IF NOT EXISTS ux_slate_cache_season_week
  ON slate_cache (season, week);