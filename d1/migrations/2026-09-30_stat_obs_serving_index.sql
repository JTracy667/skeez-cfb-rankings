-- Task 5 — serving index for the analytics archive read. APPLIED 2026-10-01 to production
-- (cfb-history), authorised by Jeff after the v63 code deploy.
--
-- EVIDENCE OF EFFECT (production, read-only EXPLAIN QUERY PLAN, before -> after):
--   before: SEARCH o USING INDEX ix_stat_obs_season_key_week (season=?)
--   after:  SEARCH o USING INDEX ix_stat_obs_serving (season=? AND week=? AND subject_type=? AND recorded_at=?)
-- Both selectors (new `recorded_at = ?` and old `MAX(recorded_at)`) now seek the index.
-- Live /api/analytics warm latency observed 4.16-4.58s before -> 1.67-2.06s after
-- (not a controlled benchmark: in-process caching and container wake state vary; the
-- definitive evidence is the plan change above).
--
-- Rollback is one statement and is safe in either direction:
-- the index is a pure read optimisation and no query depends on its existence.
--
-- WHY IT IS JUSTIFIED (measured 2026-09-30, scripts/analytics_query_plan.py)
-- Both the old and the new selector currently narrow only on `season`:
--     SEARCH o USING INDEX ix_stat_obs_season_key_week (season=?)
-- i.e. every serving request scans the whole season partition (443k rows at the current
-- 13 pulls x 34.5k rows/pull). With this index the same queries become
--     SEARCH o USING INDEX ix_stat_obs_serving (season=? AND week=? AND subject_type=? AND recorded_at=?)
--
--   local, density matched to production (443,300 rows)   median   rows returned
--     shipped indexes:  new selector 111.7 ms | old selector 135.4 ms   34,100
--     with this index:  new selector  19.0 ms | old selector  18.9 ms   34,100
--
-- Production's own read-only EXPLAIN QUERY PLAN (scripts/analytics_query_plan.py --prod)
-- shows the same season-wide scan, so the conclusion is not a local artefact.
--
-- COST: one index on an append-only table that gains ~35k rows per pull (~1-2 pulls/day).
-- The unique index ux_stat_obs_subject already covers (subject_type, subject_id, season,
-- stat_key, week, recorded_at); this one covers the SERVING filter order instead.

CREATE INDEX IF NOT EXISTS ix_stat_obs_serving
  ON stat_observations(season, week, subject_type, recorded_at);

-- ROLLBACK:
--   DROP INDEX IF EXISTS ix_stat_obs_serving;
-- Nothing reads this index by name; dropping it only returns serving to the season scan.