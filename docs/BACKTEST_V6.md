# BACKTEST — V6 base + add-on #1 (explosiveness)

**Backtest only.** Nothing in this document has been applied to the live app, its weights, or
production. Prod remains v28, unchanged.

**Inputs:** 5-year frozen set, 4,376 games (2021–2025). Fit 2021-24, held out 2025.
**Runner:** `python scripts/backtest_v6.py --seasons 2021 2022 2023 2024 2025 --holdout 2025`
**Reports:** `data/backtest_study/v6.json`, `data/backtest_study/v6_expl_wide.json`

## STEP 1 — V6 base locked

The work-order weights sum to **0.97**, not 1.0 — they are the V5 standardized regression shares,
and the missing 0.03 was the dead Level 2 block (havoc .017 / explosiveness .006 / rest .006) that
V5 retired. A composite that does not total 1.0 is silently mis-scaled, so the four were
renormalized by 1/0.97:

| input | as given | V6 used |
|---|---|---|
| SP+ | 0.18 | **0.185567** |
| efficiency | 0.30 | **0.309278** |
| talent | 0.31 | **0.319588** |
| experience | 0.18 | **0.185567** |
| **sum** | 0.97 | **1.000000** |

HFA 3.55 · calibration power 1.1, slope 0.6378 · Level 2 empty. HFA is applied neutral-aware
(0 on neutral-site games, from the `neutral_sites` cache); flat-HFA is available via
`--no-neutral-hfa` and shows in the report.

### Does the V5 regression reproduce? Yes on the coefficients, no on the behaviour.

Control arm re-fits the V5 OLS on the Level 1 inputs (fit 2021-24):

| coefficient | V5 ARM 1 | V6 control | Δ |
|---|---|---|---|
| intercept (HFA) | +3.5513 | **+3.554** | +0.003 |
| d_sp | +0.1625 | **+0.1643** | +0.002 |
| d_eff | +0.2415 | **+0.2417** | +0.000 |
| d_tal | +0.2786 | **+0.2782** | −0.000 |
| d_exp | +0.3595 | **+0.3620** | +0.003 |

The regression reproduces to three decimals. **But the V6 base is not the regression** — it puts
those weights through the V3 family's power/slope transform, and that transform shrinks the margin:

| arm | ATS | O/U | SU | MAE vs book | \|m\| vs book | bias | band | held-out 2025 |
|---|---|---|---|---|---|---|---|---|
| **v6-base** | 50.8% | 52.2% | 69.9% | **6.67** | 9.62 vs 12.38 | **−1.02** | 2.8pp | **49.7%** |
| control v5-ols | 50.9% | 52.2% | 70.0% | 6.95 | 10.24 vs 12.38 | −0.59 | **1.0pp** | **51.0%** |

Per-season ATS — v6-base: 49.3 / 51.9 / 52.1 / 51.0 / 49.7. Control: 50.8 / 50.5 / 51.5 / 50.5 / 51.0.

**Read:** V6 base buys 0.28 of MAE on the pooled fit but loses 1.3pp on the holdout, triples the
season-to-season band (1.0 → 2.8pp) and carries a **−1.02 pt home bias** the linear model does not
have. The slope 0.6378 was fitted inside the *V3 formula family* (V3's own composite scaling), not
against this normalized-share composite, so the base comes out under-scaled. **The base's scale is
not yet right**, and that matters before any add-on weight is read as a signal.

## STEP 2 — add-on #1: explosiveness (weekly, prior weeks only)

Metric: 20+ yd explosive-play rate per team per week, averaged over weeks **strictly before** the
game, home minus away. Source: `data/playbyplay_YYYY.json` (ARM 2 harvest). Coverage 3,652 of 4,376
games with prior-week data for both teams.

| arm | ATS | O/U | SU | MAE | bias | band | held-out |
|---|---|---|---|---|---|---|---|
| base (same 3,652-game subset) | 50.8% | 52.4% | 69.5% | 5.99 | −0.11 | 3.2pp | 49.3% |
| + explosiveness, w=20 (0-20 grid) | 50.8% | 52.4% | 69.9% | 5.92 | −0.11 | 3.4pp | 49.7% |
| + explosiveness, w=50 (0-80 grid) | 50.5% | 52.4% | 69.6% | **5.86** | −0.10 | 3.3pp | 50.0% |

**Verdict: no usable signal, and the optimum is not stable.**

- The 0-20 grid pinned the weight at **20.0 — the grid edge**, i.e. it wanted more. Extending to
  0-80 moved the optimum to **50.0**: a 2.5× swing in the fitted weight from a grid choice alone.
- Across the entire 0-80 grid the fit MAE spans just **5.5204 - 5.626 (0.11 pts)**. The objective is
  flat: it has no preference worth acting on.
- Typical contribution at w=50 is only **±0.86 pts** (median |d| 0.0171); p90 ±2.2 pts.
- Raising the weight to the MAE optimum makes **ATS worse** (50.8 → 50.5) and SU worse. That is the
  shrinkage signature: MAE rewards pulling margins toward the middle, not learning a signal. This is
  the same trap `fit_slope()` documents — ATS alone is gameable by shrink, and so, it turns out, is
  MAE when the added term is nearly noise.
- Held-out 2025 moves 49.3 → 50.0, which is one to three games out of 619 — inside noise.

**Do not carry explosiveness forward as a weighted input on this result.** The V5 residual-model
coefficient (+10.97) that motivated it was fitted jointly with six features on the training seasons;
tested alone against a proper control it does not hold up.

## What this implies for the next work order

1. **Fix the base's scale first.** Refit slope (and HFA) *inside* the V6 form by MAE, the way V3's
   slope was fitted inside its own family. Until then every add-on weight is being read against an
   under-scaled base with a −1.02 bias.
2. **Gate add-ons on a stability test, not a single fitted weight.** Require: the optimum is
   interior to the grid, the MAE surface is not flat (span >> the improvement claimed), and ATS does
   not degrade. Explosiveness fails all three.
3. Offensive EPA is next per the work order, and should be run under those three gates.

## Reproduction

```bash
python scripts/backtest_v6.py --seasons 2021 2022 2023 2024 2025 --holdout 2025 \
    --out data/backtest_study/v6.json
python scripts/backtest_v6.py --seasons 2021 2022 2023 2024 2025 --holdout 2025 \
    --expl-max 80 --expl-step 2 --out data/backtest_study/v6_expl_wide.json
```