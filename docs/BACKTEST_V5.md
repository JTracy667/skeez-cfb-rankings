# BACKTEST — V5 (ARM 3 combo · ARM 1 regression · ARM 2 play-by-play)

**Inputs:** 5-year frozen set, 4,376 games (2021–2025). Fit 2021-24, held out 2025.
**Runner:** `python scripts/backtest_v5.py --seasons 2021 2022 2023 2024 2025 --holdout 2025 --arms 3,1,2b`
**Reports:** `data/backtest_study/v5_arm3.json`, `v5_arm1.json`, `v5_arm2b.json` · model_version `ca2f071afda`
**Standing rule honoured:** every fitted scale/parameter minimises **MAE**; ATS is reported, never optimised.
Break-even for reference: **52.4%**.

---

## 1. HEADLINE

**No arm beats break-even out-of-sample.** Phase 2's conclusion holds after three more attacks on it.
What V5 does deliver: a **coefficient audit of V2's hand weights** (two of the four are meaningfully
mis-set), a **killed overfit** (GBM's apparent 54.5%), and a **measured verdict on play-by-play data**
(it adds calibration, not edge).

---

## 2. ARM 3 — quick combo (V3 best-ATS params × V4 best-calibration params)

Run under BOTH formula families, because V3 carries havoc/explosiveness/rest adjustments + HFA 2.5
while V4's arm 1 is the bare nonlinear gap. Comparing a power change across families would have
confounded two variables.

**Family: V3 with adjustments** (the real comparison)

| config | ATS | held-out 2025 | band | MAE vs book | SU |
|---|---|---|---|---|---|
| v3-best-ats (power 1.7, slope refit → 0.0902) | **51.3%** | 51.2% | 2.3pp | 7.42 | 69.5% |
| v4-best-cal (power 1.1, slope 0.3912) | 50.5% | 51.2% | 4.3pp | 7.80 | 69.4% |
| **hybrid (power 1.7 + slope 0.3912)** | 49.7% | 49.7% | 5.7pp | **25.22** | 69.1% |
| v3-refit-1.1 (power 1.1, slope refit → 0.6378) | 50.5% | 51.9% | 2.8pp | **6.96** | 69.3% |

**ANSWER: combining the two does NOT improve either metric — the hybrid is degenerate.** MAE blows up
to 25.22 (mean |margin| 32.4 vs the book's 12.4) because a slope is **not transferable across powers**:
`|gap|^1.7` scales very differently from `|gap|^1.1` for large gaps, so a slope fitted at one power
produces wild margins at the other. **Slopes are only meaningful together with their power**, and
V4's 0.3912 is additionally a gap-only, movement-inclusive fit — a third reason it does not transfer.

The useful result: at power 1.7 the MAE-optimal slope is 0.0902 (ATS 51.3%), while at power 1.1 it is
0.6378 (MAE **6.96**, the best calibration anywhere in V5). Best ATS and best calibration are at
different powers and neither wins both.

---

## 3. ARM 1 — regression trained on actual margin

Target: actual margin (home − away). Features: V2 Level 1 (SP+, efficiency, talent, experience) and
Level 2 (havoc, explosiveness, rest), all home-minus-away. Train 3,435 games / 2021-24.
Missing inputs imputed with train means (d_havoc 375, d_expl 591, d_rest 409 of 3,435).

**OLS coefficients** (points of margin per unit of the 0-100 composite input):

| feature | coef | 95% CI | standardized share | V2 hand weight | verdict |
|---|---|---|---|---|---|
| intercept | +3.55 | [+2.95, +4.15] | — | (HFA fixed at 2.5) | significant |
| d_sp (SP+) | +0.163 | [+0.118, +0.208] | **0.179** | **0.35** | significant |
| d_eff (efficiency) | +0.242 | [+0.203, +0.281] | **0.296** | **0.30** | significant |
| d_tal (talent) | +0.279 | [+0.245, +0.312] | **0.314** | **0.20** | significant |
| d_exp (experience) | +0.360 | [+0.290, +0.429] | **0.182** | **0.15** | significant |
| d_havoc | −5.72 | [−17.16, +5.72] | 0.017 | (3.0 pts) | **NOT significant** |
| d_expl (explosiveness) | −0.40 | [−2.60, +1.79] | 0.006 | (3.0 pts) | **NOT significant** |
| d_rest | −0.032 | [−0.217, +0.152] | 0.006 | (0.3 pts/day) | **NOT significant** |

R² 0.326, residual sd **17.43**.

**FINDINGS — the regression disagrees strongly with V2, exactly where the work order suspected:**
1. **Efficiency is right**: 0.296 fitted vs 0.30 hand-set. That weight is earned.
2. **Talent is under-weighted by ~57%**: 0.314 vs 0.20. In the data, recruiting talent carries more of
   the margin than SP+ does.
3. **SP+ is over-weighted by roughly 2×**: 0.179 vs 0.35. V2's largest weight is its fourth-largest
   signal.
4. **Experience is slightly under-weighted**: 0.182 vs 0.15.
5. **The whole Level 2 block earns nothing**: havoc, explosiveness and rest are all statistically
   indistinguishable from zero (CIs straddle 0), which independently confirms V3's and V4's rejections
   of those terms. They should not be carrying points in the live formula.
6. **Home advantage measured from data is +3.55 pts**, not the hand-set 2.5 (+/−0.30 at 95%).

**Model quality and the killed overfit.** OLS pooled ATS 50.9% (band **0.7pp**), MAE vs book 6.96.
Ridge 50.8%. Gradient boosting *looked* like a find — 54.5% pooled, 52.7% on the 2025 holdout — but
**leave-one-season-out destroys it**:

| held-out season | OLS OOS ATS | GBM OOS ATS |
|---|---|---|
| 2021 | 50.3% | 47.4% |
| 2022 | 49.6% | 48.9% |
| 2023 | 51.5% | 51.1% |
| 2024 | 50.4% | 49.5% |
| 2025 | 51.0% | 52.7% |
| **mean** | **50.6%** | **49.9%** |

The 54.5% was produced by scoring seasons that were **in** the training set. Out of sample the GBM is
worse than OLS. Ridge and GBM buy nothing here; the linear model is the honest one.
MAE vs ACTUAL margin (the irreducible-noise check): OLS 13.75 train / 15.15 holdout, GBM 12.76 / 14.54.

---

## 4. ARM 2 — play-by-play harvest: PROBE FIRST, THEN PULL

**Probe (1 call, as instructed):** `GET /plays?year=2024&week=8` → **HTTP 200, 12.0 MB, 19,574 rows.**
Extrapolated full pull: **~1.47M rows / ~900 MB across 75-80 calls.** Verified the endpoint was NOT in
`docs/CFBD_API_MAP.md`, so this probe is its first entry.

**Verdict: feasible on calls, impossible on disk.** 80 calls against ~25K remaining monthly quota is
nothing; 900 MB of raw JSON is not worth keeping, so raw plays are aggregated per week and discarded,
and only metrics are cached (`data/playbyplay_YYYY.json`, ~1 MB/season). Harvest completed in **101
seconds**; 80 week-records, 0 failures (2021-23 have an empty week 16 — no games, recorded as empty).

Metrics per team per week: plays/game, EPA overall, EPA on early downs, EPA pass/rush, explosive-play
rate (20+ yd), havoc rate (sacks, INTs, fumbles forced/recovered, blocked kicks, negative-yard rushes),
EPA allowed, seconds/play measured from the play clock.
Two measurement fixes worth noting: counting every `down>0` play inflated plays/game ~25% (Tennessee
97 → 73 real snaps) because kicks, punts, penalties and timeouts carry a down; and the feed's spread
outcomes are not home-first, so the earlier positional read was wrong.

**ARM 2b — the metrics as Level 2 inputs on V3 arm 1 (power 1.7).** Coverage: **3,652 of 4,376** games
have prior-week play-by-play for both teams (weeks strictly BEFORE the game's own week — using the
same week would leak the game's own plays into its own projection).

| config | ATS | held-out 2025 | band | MAE vs book | SU |
|---|---|---|---|---|---|
| baseline arm1@1.7 (same subset) | 51.3% | 51.0% | 1.3pp | 6.54 | 69.2% |
| + fitted residual model (OLS on the residual) | 51.0% | **52.1%** | 2.0pp | 6.40 | 69.7% |
| + standardized composite (MAE-fit scale 0.5) | **51.4%** | **52.1%** | 1.3pp | 6.45 | 69.0% |

Residual-model coefficients: explosive rate **+10.97**, offensive EPA **+3.54**, defensive EPA
**−1.51**, early-down EPA −1.07, havoc rate −0.07 (≈0), seconds/play +0.03 (≈0).

**FINDING: play-by-play data buys CALIBRATION, not edge.** ATS moves 51.3% → 51.4% (noise);
MAE improves 6.54 → 6.45 and the held-out season improves 51.0% → 52.1%. Still under break-even
pooled. The signal is concentrated in **explosiveness and offensive EPA** — the two situational
EPA terms fight each other, and pace/havoc contribute nothing, which is a third independent
confirmation that havoc is not a useful margin input.

---

## 5. WHERE THIS LEAVES THE MODEL

- V2's weight split does not survive contact with a regression: **talent up, SP+ down, efficiency
  confirmed**. That is a **CFO/Jeff decision** — weights are doctrine and no weight was changed here.
- The Level 2 terms in the live formula (havoc, explosiveness, rest) are **not significant in any of
  the three independent tests** across V3, V4 and V5. Their points are not earning anything.
- The live HFA of 2.5 is **2.5 pts under** the measured 3.55 (CI ±0.30).
- Calibration is available and cheap: power 1.1 / slope 0.6378 gives MAE 6.96 against the live
  formula's 10.42 — but it is a site-visible number change, so it waits for sign-off.