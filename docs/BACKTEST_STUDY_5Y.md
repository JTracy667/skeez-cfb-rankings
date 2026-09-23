# 5-Year Backtest Study — 8-arm work order

**Run:** `scripts/backtest_study.py --seasons 2021 2022 2023 2024 2025 --holdout 2025`
**Report:** `data/backtest_study/study_final.json`
**Window:** 2021–2025, identical frozen inputs (PIT weekly stats + PIT priors, no re-fetch between arms)
**Decisions:** 3,791 FBS matchup decisions pooled (plus non-FBS, reported separately)

---

## 1. The headline

**No arm in the work order beats the break-even rate.** At standard −110 juice you need **52.4%**
to profit. The best arm scores 51.6%.

| arm | ATS (FBS) | FBS % | non-FBS % | O/U % | SU % | MAE vs book | fitted |
|---|---|---|---|---|---|---|---|
| baseline | 1846-1945 | 48.7 | 50.7 | 51.8 | 68.1 | 10.42 | — |
| no-srs | 1841-1950 | 48.6 | 51.8 | 51.9 | 68.1 | 10.46 | — |
| low-srs | 1836-1955 | 48.4 | 51.9 | 51.9 | 68.3 | 10.43 | — |
| rest-days | 1870-1921 | 49.3 | 50.5 | 51.8 | 67.9 | 10.99 | rest=1.5 (grid edge) |
| market-anchor-open | 1826-1782 | 50.6 | 48.7 | 52.0 | 73.7 | 1.78 | scale=0.05 |
| market-anchor-close | 1887-1904 | 49.8 | 48.7 | 51.8 | 74.5 | 0.00 | scale=0.00 |
| **market+rest-open** | **1861-1747** | **51.6** | 46.7 | 52.0 | 73.2 | 2.92 | scale=0.05, rest=1.3 |
| market+rest-close | 1934-1857 | 51.0 | 49.1 | 51.8 | 74.3 | 0.14 | scale=0.00, rest=0.1 |

Consistency check — baseline per season: 49.1 / 47.1 / 51.9 / 46.1 / 49.4. That is a coin flip
that wanders, not a signal.

## 2. What this means for the weight conversation

| question | answer | evidence |
|---|---|---|
| Does SRS deserve its 12%? | **No — and it costs nothing to remove.** | baseline 48.7% → no-srs 48.6% → low-srs 48.4%. Δ ≤ 0.3pp, i.e. noise. |
| Can weights be refit to an edge? | **Not demonstrably.** | Two structural re-weightings move ATS by <0.3pp. The ATS surface is flat in weights. |
| Is the totals edge real? | **NO — it was 2026-only.** | 5-season pooled O/U **51.8%**, against 60.4% for 2026 alone (144 decisions). |
| Does rest differential help? | **Marginally, unreliably.** | 49.3% vs 48.7% baseline; fitted factor sits at the **grid edge (1.5)**, sign of a flat/noisy surface. |
| Does the market anchor work? | **It works because of the line, not the model.** | Fitted `scaling_factor` collapses to **0.05** (open) and **0.00** (close) — the composite contributes ~nothing. |

## 3. The market anchor — the leakage diagnostic CEO asked for

Both anchors were run. The gap is the finding:

- **close-anchored:** MAE vs book = **0.00** — by construction, exactly as predicted. It is
  scoring a line against itself. Unusable as a model, kept only as the diagnostic.
- **open-anchored:** 50.6% ATS with `scaling_factor` fitted to **0.05**.

Since the fitted composite contribution is ~0, the open-anchor's 50.6% is **the open→close line
drift**, not our edge. The diagnostic therefore says: **any apparent market-anchor edge is line
movement, not model skill.** A fitted factor of 0.05 on a 0.0–1.0 grid means the honest reading is
"we could not find a weight for our composite in this formula".

Held-out (fitted 2021–2024, scored on 2025):

| arm | in-sample | held out 2025 |
|---|---|---|
| rest-days | 49.2% | 49.7% |
| market-anchor-open | 50.4% | 51.7% |
| market-anchor-close | 50.0% | 48.8% |
| market+rest-open | 51.9% | 50.4% |
| market+rest-close | 51.1% | 50.8% |

`market+rest-open` drops 1.5pp from in-sample to held-out — the classic signature of a fitted
parameter absorbing noise rather than signal.

## 4. Caveats that must travel with these numbers

1. **2021 inherits a COVID prior.** Its week-1 prior is the 2020 season's finals; 2021's own
   results (49.1% baseline) are distorted at the edges and should not be over-read.
2. **Open-line arms cover 694 of 784 FBS games** (2025): some games have no `spreadOpen`.
   Close-line arms cover all 784. The two are not scored on identical game sets.
3. **The close-anchored MAE is meaningless by design** — do not put it in a comparison column.
4. Fitted factors hitting a grid edge (rest=1.5 of 0.0–1.5) mean the optimum may lie outside the
   grid, or, more likely, that there is no real optimum.

## 5. Not run — and why

- **fresh-srs** — needs a point-in-time SRS solver (per-week, leak-free). Given `no-srs` and
  `low-srs` both land within 0.3pp of baseline, this arm cannot change the conclusion; it is
  deprioritised on that basis, not on difficulty.
- **refit** (grid/coordinate descent over the weight simplex) — machinery is in place. The two
  structural weight arms above already answer the question it was meant to answer: the ATS
  surface is flat in the weights. If it is still wanted, it needs held-out validation to be
  meaningful, and its in-sample optimum will be noise.

## 6. Reproduce

```bash
python scripts/backtest_study.py --seasons 2021 2022 2023 2024 2025 --holdout 2025
python scripts/backtest_study.py --list-arms        # (see ARMS in the script)
```
Inputs are frozen in `data/backtest_cache/`; no network is touched once cached.