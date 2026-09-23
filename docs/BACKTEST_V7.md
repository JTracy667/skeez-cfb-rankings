# BACKTEST — V7: V6.1 scale refit + gated offensive-EPA add-on

**Backtest only.** Nothing here has been applied to the live app, its weights, or production.
Prod remains v28, unchanged.

**Inputs:** 5-year frozen set, 4,376 games (2021–2025). Fit 2021-24, held out 2025.
**Runner:** `python scripts/backtest_v7.py --seasons 2021 2022 2023 2024 2025 --holdout 2025`
**Report:** `data/backtest_study/v7.json`

Formula family (V6, unchanged): `margin = copysign(|D|^1.1, D) * slope + HFA * home`, with
`D = 0.185567*SP+ + 0.309278*EFF + 0.319588*TAL + 0.185567*EXP`.

Objective: MAE vs the book line — the same objective V3/V4's slopes were fitted with
(`backtest_study.evaluate`: `abs_err = |model_margin - book_margin|`).

## STEP 1 — V6.1 scale refit

Joint grid, slope 0.10-1.50 step 0.05 x HFA 2.00-5.00 step 0.25, minimising fit-season MAE.

**Fitted: slope 0.65, HFA 3.50** (fit MAE 6.3417). The V5 values were 0.6378 / 3.55 — so the refit
moves the parameters by **0.012 and 0.05**, and the reported MAE is unchanged at 6.67. **V6's scale
was not mis-set; those values were already the MAE optimum in this family.**

Genuine interior optimum, not a flat surface: the grid swings **6.3417 to 13.6417** (7.30), and both
neighbours on each axis are worse — slope- 6.3900, slope+ 6.3872, HFA- 6.3470, HFA+ 6.3432. Top 5
points all at slope 0.65.

| arm | ATS | O/U | SU | MAE vs book | \|m\| vs book | bias | band | held-out 2025 |
|---|---|---|---|---|---|---|---|---|
| **v6.1-base** (0.65 / 3.50) | 50.6% | 52.2% | 69.9% | 6.67 | 9.76 vs 12.38 | −1.03 | 2.1pp | 49.9% |
| v6.1-base zero-bias (0.65 / 4.157) | 50.6% | 52.2% | 69.9% | 6.67 | 10.01 vs 12.38 | −0.42 | 2.0pp | 50.2% |
| v6.0 (before refit, 0.6378 / 3.55) | 50.8% | 52.2% | 69.9% | 6.67 | 9.62 vs 12.38 | −1.02 | 2.8pp | 49.7% |

Per-season ATS — v6.1: 49.6 / 51.1 / 51.7 / 50.9 / 49.9. Zero-bias: 49.3 / 51.3 / 51.2 / 51.0 / 50.2.

### The "confirm bias near 0" check does NOT pass as specified — and here is why

At the MAE-optimal HFA the **fit-season bias is −0.605** (pooled −1.03). That is not a bug: MAE
drives the **median** residual to zero, not the **mean**. Fitting this objective leaves a small
signed offset whenever the residual distribution is skewed, and it will always prefer the
slightly-shrunken, slightly-biased point.

Bias *can* be zeroed explicitly: **HFA 4.157 gives fit-season bias +0.000 at a cost of +0.018 MAE**
(6.3417 -> 6.3598). Pooled bias then improves −1.03 -> −0.42 (2025 carries its own residual offset;
the pin was fitted on 2021-24) and held-out 2025 improves 49.9% -> 50.2%. Cheap enough that it is a
free choice, not a trade — reported as its own arm rather than silently substituted.

### "Under-scale" is not fixable without cost (so it is not a defect)

V6.1's margins are ~21% narrower than the market (|m| 9.76 vs 12.38). I tested whether closing that
gap helps. Matching the market's dispersion requires slope **0.8435** (`sd(book)/sd(nl term)` =
15.082/17.880):

| slope | HFA | \|m\| | fit MAE | pooled ATS |
|---|---|---|---|---|
| 0.50 | 3.75 | 7.94 | 6.7281 | 50.9% |
| **0.65 (MAE-opt)** | 3.50 | 9.76 | **6.3417** | 50.6% |
| 0.8435 (sd-matched) | 3.25 | **12.24** | 6.9519 | 50.1% |
| 1.00 | 3.00 | 14.28 | 8.0731 | 49.7% |
| 1.50 | 2.25 | 20.91 | 13.4371 | 50.2% |

Scale parity with the market **costs** 0.61 MAE and 0.5pp ATS. And ATS is essentially flat across
the whole slope range (49.7-50.9%). **MAE-vs-book has a built-in shrink preference: it can never be
used to "fix the scale," because being under-confident is how it wins.** The pick ordering, not its
scale, is what drives results — and that ordering carries no edge.

## STEP 2 — add-on: offensive EPA (gated)

Metric: home-minus-away prior-week mean `ppa` (CFBD per-play EPA), weeks strictly before the game.
Coverage 3,652 of 4,376 games. \|d_off_epa\| median **0.1196**, p90 0.3024.

**The gate verdict flips with the base**, which is the most important thing in this run:

| base | c | fit MAE | surface swing | G1 interior | G2 non-flat | G3 no-ATS-drop | verdict |
|---|---|---|---|---|---|---|---|
| v6.1 (HFA 3.50) | 7.0 | 5.5236 | 0.2631 | pass | pass | **FAIL** (50.7 -> 50.2) | **FAIL** |
| v6.1 zero-bias (HFA 4.157) | 7.0 | 5.5866 | 0.2734 | pass | pass | pass (50.6 -> 50.6) | **PASS** |

Same weight, same add-on, same data — a **0.65-point HFA change flips the gate**. The pass margin is
0.0pp (50.6 vs 50.6) while the two bases' own ATS differ by 0.1pp (a 4-game swing on 3,419 games).

| arm | ATS | O/U | SU | MAE | bias | band | held-out |
|---|---|---|---|---|---|---|---|
| step2 base (v6.1) | 50.7% | 52.4% | 69.5% | 5.99 | −0.14 | 2.7pp | 49.6% |
| + EPA w=7.0 | 50.2% | 52.4% | 70.0% | 5.86 | −0.11 | 2.5pp | 50.0% |
| step2 base (v6.1-zb) | 50.6% | 52.4% | 69.6% | 6.04 | +0.50 | 1.9pp | 49.7% |
| + EPA w=7.0 | 50.6% | 52.4% | 69.8% | 5.91 | +0.53 | **4.2pp** | 50.1% |

Per-season ATS — v6.1 base 49.9/50.5/52.3/51.2/49.6 -> +EPA 48.9/50.4/51.4/50.1/50.0.
Zero-bias base 49.8/50.7/51.6/51.5/49.7 -> +EPA 48.7/50.4/52.9/50.6/50.1.

**Verdict: not adopted.** On the reference arm it fails G3 outright. The zero-bias "pass" is not
evidence of signal — it is the same weight squeaking past a threshold that a 0.65-point change in the
base reverses, and it doubles the season band (1.9 -> 4.2pp). Typical contribution is only ±0.84 pts
(p90 ±2.12).

### Two more things this run surfaced

- **corr(d_explosive_rate, d_off_epa) = +0.613.** The V6 add-on (explosiveness) and this one
  (offensive EPA) are substantially the same signal. Testing them one at a time on this data is
  partly double-counting.
- **Both converge to the same fit MAE**: explosiveness 5.5204 (wide grid), offensive EPA 5.5236.
  Two "different" add-ons landing on the same optimum is what fitting the same shrink direction
  looks like — consistent with neither carrying new information.

## Implications for the next work order

1. **V6.1 is the reference arm**: slope 0.65 / HFA 3.50 / weights as normalized. It is V6.0 to two
   decimals; the base was never mis-scaled. Optionally pin HFA 4.157 to zero the bias for +0.018 MAE.
2. **Stop using MAE-vs-book as the fit objective for scale.** It structurally prefers under-confidence
   (slope 0.65, |m| 9.76 vs 12.38) and cannot be pushed to market scale without paying accuracy.
   If scale parity is wanted, set it directly (sd-match slope 0.8435) and accept MAE 6.95 — do not
   expect the fitter to find it.
3. **Add the base-robustness gate.** The current three gates were satisfied on one base and failed on
   another. Require a candidate to pass on every base variant (HFA 3.50 and 4.157 at minimum) before
   it survives, and treat ATS deltas under ~0.5pp (≈17 games) as noise, not evidence.
4. **Test correlated add-ons together.** Explosiveness and offensive EPA correlate at 0.61; if either
   is ever to be adopted it should be as a single combined term with one fitted weight, not two
   independent add-ons.
5. The composite's **ordering** carries no edge anywhere in this family (ATS 49.7-50.9 across every
   slope and both add-ons). Weight work cannot fix that; the next real move is new *inputs*, not
   rescaling the ones we have.

## Reproduction

```bash
python scripts/backtest_v7.py --seasons 2021 2022 2023 2024 2025 --holdout 2025 \
    --out data/backtest_study/v7.json
```