# D1 baseline backtest — per-stat predictive scores (2021-2025)

Generated 2026-09-22 15:15Z by `scripts/backtest_baseline.py`
Source: local export of D1 (`data/d1_export.sqlite`), 17517 scored games, 70 team-level stats.

## Read this before quoting any number

**These are NOT leakage-free out-of-sample scores.** The ingested stats are
**season aggregates** (one row per team-season, `week` 0), so each stat value
already includes the games it is being scored against. What this table measures
is how strongly a stat's *season differential* tracks the final margin — a
**ranking of statistical signal**, not a claim about predictive performance.

A true walk-forward backtest needs **weekly** stat pulls (CFBD serves them;
~84 calls for 6 seasons x 14 weeks). Until those are ingested, treat the
ordering below as a shortlist of what is worth testing properly, and nothing
more. Method: differential = home_stat - away_stat; corr vs actual margin;
`sign_acc` = share of games the differential picked the winner.

## Results

| stat_key | games | corr | sign_acc |
|---|---:|---:|---:|
| `fpi` | 3944 | 0.755 | 76.8% |
| `elo` | 3944 | 0.738 | 76.2% |
| `sp_plus` | 3944 | 0.737 | 77.0% |
| `totalYards` | 3944 | 0.570 | 70.3% |
| `firstDowns` | 3944 | 0.540 | 69.7% |
| `games` | 3944 | 0.511 | 66.0% |
| `rushingTDs` | 3944 | 0.501 | 66.9% |
| `sacks` | 3944 | 0.456 | 66.0% |
| `possessionTime` | 3944 | 0.441 | 68.4% |
| `thirdDownsOpponent` | 3944 | 0.431 | 66.5% |
| `rushingYards` | 3944 | 0.419 | 65.3% |
| `passingTDs` | 3944 | 0.418 | 63.8% |
| `thirdDownConversions` | 3944 | 0.417 | 64.8% |
| `tacklesForLoss` | 3944 | 0.402 | 64.0% |
| `talent_rating` | 5469 | 0.395 | 61.0% |
| `passAttemptsOpponent` | 3944 | 0.394 | 66.7% |
| `passesIntercepted` | 3944 | 0.374 | 63.6% |
| `interceptionsOpponent` | 3944 | 0.372 | 63.4% |
| `turnoversOpponent` | 3944 | 0.367 | 63.2% |
| `netPassingYards` | 3944 | 0.365 | 63.0% |
| `fourthDownsOpponent` | 3944 | 0.364 | 64.1% |
| `rushingAttempts` | 3944 | 0.331 | 63.6% |
| `puntReturns` | 3944 | 0.316 | 59.4% |
| `interceptionYards` | 3944 | 0.289 | 59.9% |
| `puntReturnYards` | 3944 | 0.287 | 58.7% |
| `possessionTimeOpponent` | 3944 | 0.243 | 58.8% |
| `passCompletions` | 3944 | 0.231 | 59.3% |
| `passCompletionsOpponent` | 3944 | 0.228 | 59.8% |
| `interceptionTDs` | 3944 | 0.228 | 53.1% |
| `kickReturnsOpponent` | 3944 | 0.189 | 57.1% |
| `fumblesLostOpponent` | 3944 | 0.149 | 54.6% |
| `penaltiesOpponent` | 3944 | 0.140 | 56.8% |
| `fumblesRecovered` | 3944 | 0.140 | 54.8% |
| `kickReturnYardsOpponent` | 3944 | 0.137 | 55.2% |
| `fourthDownConversionsOpponent` | 3944 | 0.130 | 54.7% |
| `passAttempts` | 3944 | 0.128 | 56.0% |
| `puntReturnTDs` | 3944 | 0.126 | 48.9% |
| `penaltyYards` | 3944 | 0.126 | 55.4% |
| `thirdDowns` | 3944 | 0.116 | 56.2% |
| `penalties` | 3944 | 0.107 | 54.4% |
| `fourthDownConversions` | 3944 | 0.078 | 52.2% |
| `penaltyYardsOpponent` | 3944 | 0.025 | 53.2% |
| `kickReturnTDs` | 3944 | 0.023 | 45.7% |
| `netPassingYardsOpponent` | 3944 | -0.008 | 52.3% |
| `rushingAttemptsOpponent` | 3944 | -0.014 | 49.4% |
| `thirdDownConversionsOpponent` | 3944 | -0.039 | 49.9% |
| `kickReturnTDsOpponent` | 3944 | -0.049 | 43.3% |
| `fumblesLost` | 3944 | -0.103 | 45.3% |
| `fumblesRecoveredOpponent` | 3944 | -0.111 | 45.5% |
| `puntReturnTDsOpponent` | 3931 | -0.113 | 42.8% |
| `fourthDowns` | 3944 | -0.114 | 45.5% |
| `kickReturnYards` | 3944 | -0.148 | 46.0% |
| `firstDownsOpponent` | 3944 | -0.156 | 46.8% |
| `interceptionYardsOpponent` | 3944 | -0.178 | 44.7% |
| `interceptionTDsOpponent` | 3944 | -0.203 | 40.8% |
| `sacksOpponent` | 3944 | -0.228 | 40.7% |
| `kickReturns` | 3944 | -0.231 | 42.7% |
| `passesInterceptedOpponent` | 3944 | -0.245 | 41.4% |
| `interceptions` | 3944 | -0.245 | 41.4% |
| `turnovers` | 3944 | -0.246 | 42.1% |
| `tacklesForLossOpponent` | 3944 | -0.250 | 41.9% |
| `puntReturnYardsOpponent` | 3931 | -0.273 | 41.4% |
| `totalYardsOpponent` | 3944 | -0.278 | 43.5% |
| `puntReturnsOpponent` | 3931 | -0.299 | 39.8% |
| `passingTDsOpponent` | 3944 | -0.322 | 41.0% |
| `rushingYardsOpponent` | 3944 | -0.376 | 38.2% |
| `recruiting_class_rank` | 5170 | -0.405 | 38.9% |
| `rushingTDsOpponent` | 3944 | -0.442 | 35.6% |
| `fcs_rating` | 499 | -0.491 | 28.9% |
| `sp_plus_rk` | 3944 | -0.717 | 22.9% |

## Blocked / honest limits

* No weekly granularity in `stat_observations` yet -> no leakage-free backtest.
* Historical line *movement* exists only from 2026 forward (risk register C1):
  CLV-style backtests before that are closing-line-only.
* FCS-specific stats remain unavailable from CFBD (silently ignores
  `division=fcs`); `fcs_rating` (Coaches Poll) is the only FCS signal stored.
