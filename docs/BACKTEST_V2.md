# MODEL V2 — backtest results (5-year, frozen inputs)

**Run:** `python scripts/backtest_v2.py --seasons 2021 2022 2023 2024 2025 --holdout 2025`
**Report:** `data/backtest_study/v2_final.json` · **Engine:** `scripts/backtest_v2.py`
**Window:** 2021–2025, identical frozen inputs. Fit on 2021–2024, held out 2025.
**Decisions:** 3,791 FBS matchup decisions pooled.

---

## 1. Headline

**v2 beats v1 on every metric, and the Level-1 re-weight is the reason.** Still short of the
52.4% break-even at −110, but the improvement is real and it holds out of sample.

| arm | ATS (FBS) | FBS % | O/U % | SU % | MAE vs book | slope / k | held out 2025 |
|---|---|---|---|---|---|---|---|
| v1 baseline (for reference) | 1846-1945 | 48.7 | 51.8 | 68.1 | 10.42 | 1.0 | — |
| v2-no-matchup (Level 1 only) | 1905-1886 | 50.3 | 52.2 | 69.4 | 7.10 | slope 0.7 | 50.3 |
| v2-no-experience | 1888-1903 | 49.8 | 52.1 | 69.5 | 7.59 | slope 0.5* | 50.6 |
| **v2-baseline** (L1 + L2) | 1926-1865 | **50.8** | 52.2 | 69.5 | 7.52 | slope 0.6 | **51.2** |
| v2-market-open | 1861-1747 | **51.6** | 52.4 | 73.7 | 1.66 | k 0.05 | 51.8 |
| refit-v2 | 1926-1865 | 50.8 | 52.2 | 69.5 | 7.52 | slope 0.6 | 51.2 |

\* slope pinned at the 0.5 grid edge — see §4.

## 2. What each result means

- **Level 1 is a genuine improvement.** 48.7% → 50.3% ATS, and **MAE 10.42 → 7.10**: v2's
  composite is materially better calibrated to the market than v1's. Dropping SRS/Elo/FPI and
  concentrating on SP+/efficiency/talent/experience pays.
- **Level 2 adds a small, real amount.** 50.3% → 50.8% pooled, and held-out improves 50.3% →
  51.2%. Prior-season havoc + explosiveness + rest are worth ~+0.5pp, not nothing.
- **Consistency improved a lot.** v2-baseline per season: 49.9 / 51.5 / 50.5 / 50.9 / 51.2 — a
  1.3pp band. v1's was 46.1–51.9, a 5.8pp band. v2 is a steadier model.
- **The market anchor now shows composite contribution**, correcting the v1 reading:
  - pure opening line, k = 0.0 → **1463-1464 = 50.0%** (the null, as theory predicts)
  - v2 composite at k = 0.05 → **1861-1747 = 51.6%**
  So the composite adds ~+1.6pp *over the line itself*. In the v1 study the equivalent arm was
  read as line drift; in v2's frame that reading is wrong.
- **The refit found nothing.** Coordinate descent over slope + Level-2 scales + Level-1 weights
  returned the spec values unchanged (weights exactly 0.35/0.30/0.20/0.15); only the slope moved,
  1.0 → 0.6. The spec is at a local optimum on these grids.

## 3. Scale findings that affect production

- **The fitted slope is 0.6–0.7, not the production 1.0.** v2's composite is more dispersed than
  v1's, so it needs a *shallower* conversion to margin. Shipping v2 while keeping slope 1.0 would
  overstate every margin by ~40%.
- **v2 is far more conservative than the market:** mean |model margin| 7.29 vs mean |book line|
  12.38. MAE is excellent (7.52) but the model systematically prices smaller margins than the
  market. That is a deliberate-looking consequence of the slope, and it is the single biggest
  remaining calibration question.

## 4. Caveats that must travel with these numbers

1. **Injury is NOT in this run.** Re-specified as a composite discount (CEO/Jeff approved), but no
   historical injury data exists — `active_injuries.json` is a single live snapshot. The term can
   only be validated on the current season, and its magnitudes are **judgment, not measured**.
2. **Wind is not wired.** Needs a `/games/weather` fetch. Totals-only, ≥15 mph.
3. **Havoc is prior-season.** In-season havoc has no point-in-time source (D1 stores the components
   at week 0 only). Leak-free, but blind to in-season defensive change.
4. **Level-2 coverage is 687–779 of 825–941 games per season** (missing opening lines / prior-week
   stats), so the Level-2 arms are scored on a slightly smaller set than Level 1 alone.
5. **2021 inherits a COVID prior** (its week-1 prior is 2020's finals).
6. **Refit is a bounded coordinate descent, 2 passes** — not an exhaustive simplex search.

## 5. Production prerequisite — a live bug

`experience_score` is corrupt in production: CFBD mixes class-year ints (1–4) with season-year
ints (2024) in the same `year` field (~5% of rows), and since 2024 is ~700× larger than 3 even a
few bad rows inflate a team's mean into the thousands. Live `cfbd_analytics.json` shows values up
to **67,500**; **60 of 303 teams affected, 6 of them FBS** (UTSA, Toledo, Air Force, Troy, Middle
Tennessee, Akron).

**Latent today** — `experience_score` feeds only the `/api/analytics` payload, never the composite.
It becomes real the moment v2 weights it 15%. `app.py:_cfbd_roster_experience` needs the same
class-year filter as `scripts/fetch_season_experience.py` before v2 ships.

## 6. Reproduce

```bash
python scripts/fetch_season_experience.py 2021 2022 2023 2024 2025   # roster experience
python scripts/fetch_season_havoc.py 2020 2021 2022 2023 2024        # prior-season havoc
python scripts/backtest_v2.py --seasons 2021 2022 2023 2024 2025 --holdout 2025
```
Inputs freeze in `data/backtest_cache/`; no network once cached.