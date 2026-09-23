# BACKTEST — V8: matchup style + coaching (both arms FAIL the gate)

**Backtest only.** Nothing applied to the live app, weights, or production. Prod remains v28.

**Base:** V6.1 (slope 0.65, HFA 3.50), variant HFA 4.157. **Gate:** interior optimum + non-flat MAE
surface + no ATS degradation, **on both bases**. Frozen 2021-25, fit 2021-24, held out 2025.
**Runner:** `python scripts/backtest_v8.py --seasons 2021 2022 2023 2024 2025 --holdout 2025`

## Data reality (checked before building — the order's field names did not match the repo)

- `off_epa_rush` **does not exist**; the field is `epa_rush` (with `def_epa_rush`).
- `cfbd_analytics.json` carries `coach_win_pct` and EPA splits, but it is a **current single
  snapshot with no season key** — unusable on 2021-25 without leaking the present into the past.
  No per-season coaches or EPA-split cache existed.
- **Built instead, point-in-time:** weekly rush/pass EPA splits for offense *and* defense
  (extended `harvest_plays.py`, re-pulled /plays: 80 calls, 5 seasons, weekly, prior weeks only);
  per-season head-coach records (`harvest_coaches.py`, /coaches, 6 calls — school lives inside
  `seasons[]`, not a top-level `team` key); trench from `lineYards` in the **prior** season's
  `raw_stats_season_advanced`.

## ARM 1 — matchup style interactions (combined term, 3,409 games)

`rush/pass/trench_matchup = (home off − away def) − (away off − home def)`, z-scored jointly.

**The three features are genuinely non-collinear** (rush~pass +0.184, rush~trench +0.202,
pass~trench +0.089) — this is new information of a new type, not a repackaging of ARM 2's material.
It still adds nothing.

| base | best c | fit MAE (swing) | ATS | MAE | bias | band | held-out | G1 | G2 | G3 |
|---|---|---|---|---|---|---|---|---|---|---|
| HFA 3.50 | 0.5 | 5.2805 (0.2964) | 50.8% → **50.6%** | 5.39 | +0.66 | 2.8pp | 49.4% | ✓ | ✓ | **✗** |
| HFA 4.157 | 0.5 | 5.3603 (0.2900) | 50.7% → **50.4%** | 5.47 | +1.30 | 1.4pp | 49.9% | ✓ | **✗** | **✗** |

**VERDICT: FAIL.** Interior on both bases, but ATS degrades on both and the MAE barely moves
(5.4 → 5.39 on the reference base). **Per the work order, not split into 3.**

## ARM 2 — coaching (coach win pct, prior season, 3,855 games)

| base | best c | fit MAE (swing) | ATS | MAE | bias | band | held-out | G1 | G2 | G3 |
|---|---|---|---|---|---|---|---|---|---|---|
| HFA 3.50 | **0.0** | 5.3874 (0.8898) | 50.7% → 50.7% | 5.5 | +0.55 | 1.8pp | 49.9% | **✗** | **✗** | ✓ |
| HFA 4.157 | **0.0** | 5.4537 (0.8779) | 50.7% → 50.7% | 5.56 | +1.15 | 1.7pp | 50.2% | **✗** | **✗** | ✓ |

**VERDICT: FAIL.** The MAE-optimal weight is **exactly 0.0** — the term is not just weak, the fit
actively wants it off. Note the surface *does* move (swing 0.89, the largest of any add-on tested),
so this is a real zero rather than a flat grid. Done, per the order.

## What V8 adds to the picture

1. **Two more independent input types rejected — 7 add-ons/features now tested, 0 survived.** Every
   one fails on **ATS first**: the ordering is what lacks edge, and no input tested changes it.
2. **Non-collinearity does not rescue a feature.** ARM 1's components are near-independent of each
   other and of previous arms' material, and still fail. That removes "we just tested the same thing
   twice" as an explanation for the run of nulls.
3. **The gate is doing its job.** In V7 a single-base gate flipped on a 0.65-pt HFA change; under the
   both-bases rule, V8's two candidates fail cleanly and are not carried forward on a technicality.
4. **Consequence for future work orders:** the next step is not another input in this family. The
   composite computes a weighted average of season-level team quality and it prices like the market
   (50-51% ATS at every slope and every weight tested). Beating it needs a different *question*
   (situational/matchup state the market is slow on, injury/lineup state, in-season price movement),
   not another column of the same table.

Reports: `data/backtest_study/v8.json`. Harvests: `data/playbyplay_*.json` (gitignored, regenerable),
`data/backtest_cache/coaches_YYYY.json`.

## Reproduction

```bash
python scripts/harvest_plays.py --seasons 2021 2022 2023 2024 2025 --force   # rush/pass splits
python scripts/harvest_coaches.py --years 2020 2021 2022 2023 2024 2025
python scripts/backtest_v8.py --seasons 2021 2022 2023 2024 2025 --holdout 2025
```