-- Migration 2026-09-24c — make slate_cache the general DERIVED-BOARD store.
--
-- The Schedule slate proved the pattern: compute a board once and serve it, rebuilding
-- only when its inputs change. Rankings and Win Totals are the same shape, and they
-- still recompute on in-memory TTLs (rankings 5 min, win totals 1h), so a container
-- that sleeps — i.e. every cold start — rebuilds them from scratch for the first
-- visitor. That is the cost this closes.
--
-- `kind` names the board: 'schedule' | 'rankings' | 'win_totals'. Season-level boards
-- use week = 0; the Schedule slate uses the real week number.
--
-- Existing rows are the Schedule slate and keep working: the new column defaults to
-- 'schedule' and the old unique index is replaced by one on (kind, season, week).
--
-- Apply with:
--   npx wrangler d1 execute cfb-history --remote --file=d1/migrations/2026-09-24c_derived_boards.sql

ALTER TABLE slate_cache ADD COLUMN kind TEXT DEFAULT 'schedule';

DROP INDEX IF EXISTS ux_slate_cache_season_week;

CREATE UNIQUE INDEX IF NOT EXISTS ux_slate_cache_kind_season_week
  ON slate_cache (kind, season, week);