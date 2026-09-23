# V3 — structural changes + selection filter (arms 1–4)

**Run:** `python scripts/backtest_v3.py --seasons 2021 2022 2023 2024 2025 --holdout 2025`
**Report:** `data/backtest_study/v3_final.json` · **Engine:** `scripts/backtest_v3.py`
Fit 2021–2024, held out 2025. Arm 5 (separate totals model) NOT run — see §6.

---

## 0. FIRST: the objective had to change, and that invalidates a prior result

**Fitting slope by maximising ATS is a gameable objective.** As slope → 0 the margin collapses
toward a constant pick ("always the home underdog"), which scores **~51% by construction**. Fitted
that way, slope came out at **0.014** with SU **61.1%** and MAE **11.66** — a degenerate model, not
a better one. Fitting by **minimising MAE** gives slope **0.81**.

Every V3 number below uses MAE-fitted parameters. Two consequences:

1. **This retroactively explains the V2 fine-tune result.** There, 3 of 5 parameters pinned at grid
   edges (slope 0.50, HFA 1.50, havoc 1.00) — all pushing the margin down — and the pooled 50.9%
   failed out of sample (LOSO 49.6%). That was this same degeneracy, not a real optimum.
2. **Methodology going forward: never fit on ATS alone.** Calibration cannot be gamed by shrinkage.

## 1. Headline

**Arm 1 works; arms 2 and 3 don't; arm 4 improves calibration but not picks. Nothing clears the
52.4% break-even.**

| arm | ATS (FBS) | FBS % | O/U % | SU % | MAE | \|m\| (book 12.38) | band | held-out 2025 | fitted |
|---|---|---|---|---|---|---|---|---|---|
| v2-baseline (MAE-fit ref) | 1907-1884 | 50.3 | 52.2 | 69.3 | 6.97 | 9.53 | 3.0pp | 51.5 | slope 0.8144 |
| **arm1-nonlinear** | 1946-1845 | **51.3** | 52.2 | 69.5 | 7.42 | 8.21 | **2.3pp** | 51.2 | **power 1.7** (interior), slope 0.0902 |
| arm1-gapclosing | 1892-1899 | 49.9 | 52.2 | 69.5 | 8.83 | **12.46** | 3.4pp | 50.2 | slope set to match book |
| arm2 v2-baseline | flat | 49.8–50.6 | — | — | — | — | — | 51.2–52.7 | thresholds 0–7 |
| arm2 market | 50.9–51.8 | — | — | — | — | — | — | 50.9–57.4 | thresholds 0–7 |
| arm3-combined | 1474-1441 | 50.6 | — | — | — | — | — | 51.4 | power 1.7 + threshold 2 |
| arm4-linemove | 1897-1894 | 50.0 | 52.2 | 69.8 | **6.71** | 9.77 | 2.4pp | 51.2 | move_factor 0.8 (interior) |

## 2. Arm 1 — the nonlinear curve works, but NOT for the stated reason

Power **1.7** (interior of the 0.8–2.0 grid, not an edge) with slope 0.0902 gives the best
model-frame ATS: **51.3%**, held-out **51.2%**, and the **tightest per-season band of any arm
(2.3pp)** — more than a full point better than the linear baseline's 3.0pp.

**But the stated target was wrong.** Arm 1 was asked to close the `mean|margin|` gap (7.29 vs
12.38). Forcing that — `arm1-gapclosing`, slope set so |margin| = 12.46 against a book of 12.38 —
is strictly worse: ATS falls **51.3% → 49.9%**, held-out **51.2% → 50.2%**, MAE worsens
**7.42 → 8.83**.

**The compression is not a defect.** The model is better calibrated at its own, tighter scale.
Matching the market's spread costs accuracy. Any future work should stop treating
`mean|margin| < mean|book|` as a problem to fix.

## 3. Arm 2 — does the model do better when confident? No.

| threshold | model frame (|edge|) | market frame (|edge|) | market (|model−book|) |
|---|---|---|---|---|
| ≥0 | 50.3% (3866) | 51.6% (3682) | 51.6% (3682) |
| ≥1 | 50.3% (3624) | 51.8% (3456) | 49.5% (2004) |
| ≥3 | 50.6% (3170) | 51.0% (3026) | 49.9% (600) |
| ≥5 | 50.0% (2747) | 50.9% (2626) | 53.1% (199) |
| ≥7 | 49.8% (2356) | 51.0% (2252) | 48.0% (77) |

Model frame is **flat within noise** across the whole 0–7 range (49.8–50.6%). The market frame
peaks at ≥1 (51.8%, held-out 52.4%) and then declines. The eye-catching high-threshold entries
(53.1% on 199 picks, held-out 57.4%) sit on **77–199 picks** — small-sample noise, not signal.

## 4. Arm 3 — the intended ship candidate does not work

Combined power 1.7 + threshold 2 gives **50.6%** — **worse than arm 1 alone (51.3%)**. Adding the
filter destroys value. There is no ship candidate from this work order.

## 5. Arm 4 — line movement improves calibration, not selection

`move_factor` fitted to **0.8** (interior) gives MAE **6.71 — the best calibration of any arm run
so far** (v2-baseline 6.97, arm1 7.42). But ATS is 50.0% (held-out 51.2%) — calibration improved,
picks did not.

**Structural caveat:** `(open_spread − close_spread)` is derived from the two lines themselves, so
this term is not new information — it is a partial re-anchoring toward the opening line, which is
already known to help calibration (that is what the market frame does). It should not be reported
as "sharp money" signal.

## 6. Arm 5 — not run

The separate totals model needs (a) a decision on which inputs and their scales — `pts_per_poss`
and `off_ypp`/`def_ypp` have no obvious linear mapping into points — and (b) a `/games/weather`
fetch for the wind term. Guessing at the scales would produce exactly the kind of confident wrong
number this program exists to prevent.

## 7. Reproduce

```bash
python scripts/backtest_v3.py --seasons 2021 2022 2023 2024 2025 --holdout 2025
```