# BACKTEST — V4 PHASE 2 (arms 1–3)

**Run:** `python scripts/backtest_v4.py --seasons 2021 2022 2023 2024 2025 --holdout 2025`
**Report:** `data/backtest_study/v4_phase2.json` · model_version `ca2f071afda` · 4,376 games
**Fitting rule enforced:** every fitted parameter minimises **MAE**; ATS is reported, never optimised.

---

## 1. HEADLINE

**Nothing here clears the 52.4% break-even.** Two holdout subsets do (53.1% / 53.2%), but both sit
inside wide per-season bands, so neither is a ship candidate. What Phase 2 *did* produce is a
calibration win and two negative results that close off branches.

| arm | pooled ATS | held-out 2025 | band | MAE vs book | SU |
|---|---|---|---|---|---|
| ref-production (live model) | 48.7% | 49.4% | 5.8pp | 10.42 | 68.1% |
| **arm1-power+move** (power 1.1, slope 0.3912, move −0.5) | 49.3% | 49.9% | **2.6pp** | **7.99** | 67.3% |
| arm1-move-present (subset) | 50.4% | 51.1% | 2.6pp | 7.57 | 66.7% |
| arm1-hisbook-move (subset) | **50.9%** | **53.1%** | 6.6pp | 6.94 | 67.9% |
| arm2-hisbook-open-anchor (k=0.0) | 49.8% | 51.8% | 3.8pp | 1.18 | 73.9% |
| arm2-graded-vs-his-close | 50.9% | 53.1% | 6.0pp | 0.87 | 73.8% |
| arm2-betonlineag-only | 51.2% | **53.2%** | 5.7pp | 0.87 | 73.5% |
| arm2-williamhill-only | 49.8% | n/a (1 game) | 61.4pp | 0.85 | 74.5% |
| arm3-totals-fit | 48.5% | 49.4% | 4.8pp | 9.44 | 67.6% |
| arm3-ref-production (totals) | 48.5% | 49.4% | 4.8pp | 9.44 | 67.6% |

## 2. FINDINGS

**ARM 1 — the movement term does not survive combination.** `move_factor` fitted to **−0.5, the grid
edge**, i.e. the optimiser wants to move as far from "movement helps" as the grid allows. That is the
same class of result V3 produced for the stand-alone movement arm, now confirmed when combined with
the nonlinear curve. Power also moved 1.7 → **1.1** once movement was in the frame.

**ARM 1's real gain is calibration, not ATS.** MAE 10.42 → **7.99** and the per-season band tightens
5.8pp → **2.6pp**. The 0.3912 slope is a far better-calibrated margin than the live one, and it is the
single most defensible number Phase 2 produced.

**ARM 2 — in the market-anchor frame the composite contributes nothing.** The MAE-fitted `k` collapsed
to **0.0 (grid edge)**. With k=0 the arm degrades to a pure fade-the-opening-move rule, and its
49.8% is therefore *not* evidence about the composite at all. Honest reading: anchoring on his opening
line beats the model frame on calibration (MAE 1.18) but yields no edge.

**Jeff's William Hill rule holds up on the fallback subset.** BOL-only 51.2% vs William-Hill-only
49.8% pooled — no degradation from the fallback. The 61.4pp band / 0.0% holdout on the WH subset is a
sample-size artifact (2023: 18 games, 2025: **1** game), not a finding. Coverage is the real win:
**2,871** games have a bettable opener under the chain, vs 2,140 BOL-only, and it is what puts 2021 in
the arm at all (581 games; BOL has **no 2021 archive** in The Odds API).

**ARM 3 — the fitted totals model is a wash.** b0 22.0, b1 0.55, b2 0.60 (none at a grid edge),
fit MAE 5.327, O/U **52.6%** vs production's 52.1% on the same 3,756 games. +0.5pp is inside noise;
ATS/SU are unchanged because a totals model cannot move the margin.

## 3. TWO TRAPS THAT FABRICATED A RESULT (do not repeat)

1. **Positional outcome access flips the line.** The Odds API does **not** guarantee home-first
   ordering — observed `[{Coastal Carolina +9.5}, {James Madison −9.5}]`. Taking `spreads[0]` negated
   the line for a large minority of games and made the model appear to score **59.6% ATS** against a
   line that was not its opponent's price. Resolve the spread **by team name** (`_home_spread`).
2. **A team-pair-only join attaches other games' lines.** Without season + kickoff agreement the join
   pulled in wrong games: `|his close − CFBD close|` p90 **40 pts**, max **109.5**. Both fixes are in
   `join_lines` now, and the report carries the counters that caught it —
   `rejected_kickoff_mismatch` and `close_diverges_from_cfbd`.
   **Sanity rule: if this model ever appears to score >55% ATS, suspect the join before believing it.**

**Credit leak, fixed:** `import app` runs `_start_scheduler()` (app.py:5163), which fires a real
`refresh_all()` ~10s later — every backtest run was silently spending Odds API / PropLine credits and
rewriting live caches. `backtest_v4.py` now sets `REFRESH_INTERVAL_SECONDS=0` before importing app.
The same fix should be applied to the older arms scripts if they are ever re-run.

## 4. WHAT THIS DOES NOT SETTLE

- Whether to ship the arm-1 calibration (slope 0.3912, power 1.1) into the live margin formula.
  It touches a site-visible number → **needs Jeff's sign-off**, and the CFO owns the weight doctrine.
- Phase 3 (PropLine daily snapshotting) is untouched and remains forward-test only.