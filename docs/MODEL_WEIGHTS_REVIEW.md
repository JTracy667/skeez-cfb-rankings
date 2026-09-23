# Predictive Model & Weights — Review Document

**Prepared for:** Jeff, for review with CEO and CFO
**Status:** findings and options only — **no weight or model change has been applied**
**Date:** 2026-09-22

**How to reproduce every number here** (nothing in this document is from memory):

```bash
python scripts/probe_input_vintages.py    # is each input current or a season old?
python scripts/probe_api_surface.py       # endpoint transport: ETag/304, payload sizes
python scripts/selftest_composite_config.py   # weights sum to 1.0, version hash
```

Companion doc: `docs/CFBD_COMPOSITE_INPUTS.md` (field-level CFBD → composite map).

---

## 1. The model chain

```
14 CFBD endpoints
      │
      ▼
normalized inputs (0–100 each)
      │
      ▼
COMPOSITE (0–100, weighted sum)          ← weights are the CFO's lever
      │
      ├──► projected score per team
      ├──► margin  = 1.0 × (comp_home − comp_away) + 2.5 HFA + injuries (+ floor)
      ├──► total   = 51 + (avg_composite − 50) × 0.10 + injuries − wind
      └──► win prob = logistic(1/(1+e^(−0.08×(composite−50))))
```

---

## 2. The weights (the thing under review)

Source of truth: `COMPOSITE_CONFIG` in `app.py`. Weights are hashed into
`model_version` = **`ca2f071afda`**, and `_assert_weights_sum()` enforces they total 1.0.

| weight | input | what it is |
|---|---|---|
| **0.37** | Efficiency (5 sub-metrics) | on-field per-play performance |
| 0.18 | SP+ | opponent-adjusted efficiency rating |
| 0.15 | FPI | ESPN FPI score, rescaled |
| 0.12 | **SRS** | Simple Rating System — **currently last season** |
| 0.10 | Talent prior | 247 composite + recruiting + returning production |
| 0.08 | Elo | CFBD Elo |
| 0.00 | `fcs_rating` | deliberately inert (see §7.4) |
| 0.00 | `massey` | deliberately inert (unused) |

Sum = 1.00 exactly (enforced at runtime; a bad weight table raises rather than silently
skewing every projection).

**Change control (current, and the reason nothing has moved):** the code itself states a
weight change "changes what the model says, so per CFO doctrine and the checklist's own
instruction it needs **JEFF'S APPROVAL** on the weights". A change is not a code tweak —
it requires a re-fit against real results and re-validating the downstream scoring curves,
because `model_version` changes and historical predictions are no longer comparable.

---

## 3. How each input is normalized

Every input lands on 0–100 so the weights are comparable. `clamp01` = `max(0, min(100, x))`.

```
sp_norm     = clamp01((sp_plus + 40) / 80 × 100)
fpi_norm    = clamp01(fpi_win_prob)                    ← see §7.3, this is derived
srs_norm    = clamp01((srs + 25) / 50 × 100)
elo_norm    = clamp01((elo − 1500) / 300 × 100 + 50)
talent_norm = program_talent × 0.60 + ret_norm × 0.40
   program_talent = talent_comp_norm × 0.70 + rec_norm × 0.30
        talent_comp_norm = clamp01((247 talent − 250) / 750 × 100)
        rec_norm         = clamp01((130 − recruiting rank) / 129 × 100)
   ret_norm = clamp01(pct_ppa_returning or 50)
eff_norm    = sr × 0.28 + epa × 0.24 + ppo × 0.16 + trench × 0.16 + ppd × 0.16
```

The efficiency sub-metrics, in composite terms (bucket 0.37 × internal share):

| sub-metric | internal | of total | CFBD source |
|---|---|---|---|
| Net success rate | 0.28 | ~10.4% | `stats/season` → `successRate` |
| Net EPA/PPA per play | 0.24 | ~8.9% | `ppa/teams` → `overall` |
| Finishing drives (PPO) | 0.16 | ~5.9% | `stats/season/advanced` → `pointsPerOpportunity` |
| Trench dominance | 0.16 | ~5.9% | `stats/season/advanced` → `lineYards`, `stuffRate` |
| True points/possession | 0.16 | ~5.9% | computed from `drives` |

Each is a **differential** (our offense − their defense) centered so a league-average team
sits at 50. The constants in the formulas are league means from calibration.

---

## 4. What the composite turns into

```
total   = 51 + (avg_composite − 50) × 0.10        # elite games trend slightly higher
margin  = (comp_home − comp_away) × 1.0           # 1.0 pt margin per composite point
        + 2.5 if home (0 on a neutral site)
        + net injury adjustment
home    = (total + margin) / 2,  away = (total − margin) / 2
FCS branch: base_score = max(6.0, 27.0 + (fcs_composite − 50) × 0.55)
win prob: 1 / (1 + e^(−0.08 × (composite − 50)))
season win totals: logistic scale 3.0 (fitted vs 138 sportsbook O/U lines)
```

Three adjustments are applied **after** the margin, so a closed-form audit of a projected
margin will show residuals that are **not** bugs:

- **net injury** — `(home_adj − away_adj)`, the star-QB rule (−10.0 for a starting QB out)
- **wind** — total only, when sustained wind ≥ 15 mph
- **underdog floor** — when the composite gap exceeds 30, the weak side's share of the
  total is capped (~10%), which rewrites both scores and therefore moves `differential`
  after margin was computed

**ATS rule (Jeff's):** if the model margin is UNDER the spread, the underdog is the ATS side.

**FCS blowout boost:** `FCS_BLOWOUT_BOOST = 0.0` — a named zero, deliberately kept. It was
a stop-gap from when the FCS prior was a flat 16; once the composite itself carried the
FBS/FCS gap it double-counted (predicted 51.5 against a measured 37.2).

---

## 5. Findings — everything we have established

### 5.1 SRS is a full season old and carries 12% of the composite
`ratings/srs?year=2026` returns **0 rows** (266 for 2025). The code deliberately falls back
to the previous season. Measured per team: every live SRS value exactly matches the 2025
value (Georgia 16, Ohio State 26.6, Indiana 27.3, Alabama 12.7 — 10/10 sampled).

Effect: for the top 25 the SRS **point** contribution is 6.6–12.0, i.e. **8.5%–14.9% of
each team's composite** (mean 11.6%). The ranking *order* is not corrupted — 2025 SRS still
differentiates teams — it is simply a year old, and it was **unlabelled**: the analytics
page showed it as if current.

**Status:** the page now labels the vintage (`SRS (2025)`) and the API exposes the vintage.
The **weight itself is unchanged** — that is a decision for this review.

### 5.2 Missing data becomes a *neutral 50*, which is real points
Almost every gap is imputed at "league average" and then weighted, so a placeholder becomes
a real number of points in the composite:

| missing | becomes | effective points |
|---|---|---|
| SRS | norm 50 | **6.0** |
| EPA | norm 50 | 8.9 |
| success rate / PPO / trench / PPD | norm 50 | 8.9 / 5.9 / 5.9 / 5.9 |
| returning production | norm 50 | 2.0 |

**420 of the 685 teams in the analytics set have no SRS at all** (FCS and lower divisions).
For them SRS contributes a flat **6.0** — a constant, not a signal — while their FBS
opponents can get up to 12.0. That is a systematic ~6-point composite gap in FBS-vs-FCS
matchups, and it comes from a placeholder rather than from football. (This is the "6 for an
FCS school".)

### 5.3 Only ONE input is actually stale
Measured across all 14 fallback-capable endpoints for 2026: **13 are current**, SRS is the
only one served from a previous season. The staleness is one input, not a systemic
fallback — which makes it cheap to fix and worth fixing.

### 5.4 `fpi_win_prob` is not a win probability
CFBD's `/ratings/fpi` gives an `fpi` score and a `ranking`; it does **not** publish a win
probability. We derive `fpi_win_prob = clamp(0,100, 50 + fpi_score × 1.5)` and weight that
at 15%. So 15% of the composite is a linear rescale of the FPI score wearing a name that
suggests more than it is.

### 5.5 The `fcs_rating` slot is inert — and that is correct
It holds an FCS **poll rank (1–25)**, not a strength score. Given a weight, rank 25 would be
treated as stronger than rank 1 — the signal would **invert**. Leaving it at 0.0 is right;
the correct lever for FCS opponents is the missing-data prior.

### 5.6 The FCS prior (fitted, not a constant)
```
FCS_COMPOSITE_RANKED   = 33.0   (Massey rank ≤ 25)
FCS_COMPOSITE_UNRANKED = 15.0
FCS_COMPOSITE_FALLBACK = 19.0   (no rating)
```
Fitted, not chosen — and it matters which dataset is which (I had these conflated in an
earlier draft, and the correction is worth keeping visible):

- **848 real FBS-vs-FCS games** set the *targets*: FBS wins by **31.9** vs an unranked FCS
  opponent, **14.2** vs a ranked one.
- **103 completed games** are where the *error* was measured: mean absolute error
  **21.5 → 13.6 points**.

So the prior was fitted to reproduce measured margins, and its accuracy was checked on a
different, smaller set of graded games.

The old flat 16.0 is gone; a stale code comment still described it and has been corrected.

### 5.7 A single age threshold cannot police these inputs
SRS is *legitimately* a season old until CFBD publishes. Any freshness gate built on one
age threshold would fire on SRS **forever** and be ignored — or get disabled. Any
prediction-freshness rule must be **cadence-aware per input**. The payload now carries
`input_vintages` (e.g. `{"srs": 2025}`) so a gate can read the vintage instead of guessing.

### 5.8 The referenced proposal document does not exist
`app.py` points at `D1_COMPOSITE_PROPOSAL.md` as the home of the weight proposal. **That
file is not in the repo.** The decision vehicle the code claims to rely on is missing —
worth recreating as part of this review.

### 5.9 Weight stability
`_assert_weights_sum()` fails loudly if the enabled weights do not total 1.0, and the
weight table is hashed into `model_version`, so any change is detectable and any prediction
can be traced to the exact weight set that produced it. Historical `model_predictions` rows
therefore remain attributable.

---

## 6. What has been monitored since

- An hourly, window-bounded probe (Sun 18:00 → Wed 23:59 PT) watches CFBD's release
  window, with an **SRS-2026 watcher**: silent while SRS is unpublished, and it speaks up
  the moment 2026 SRS appears — so the fallback's retirement is noticed, not guessed.
- The analytics page labels the SRS vintage, and cannot label it wrongly: when the vintage
  is unknown it renders **no** label rather than a guess.

---

## 7. Decision agenda for the review

No recommendation is applied here; these are the calls that need CEO/CFO/Jeff.

1. **SRS as an input.** Options: (a) leave 12% on a season-old input and keep the label;
   (b) reduce or zero the weight until 2026 publishes; (c) leave the weight but treat SRS
   as a prior rather than a current-season signal. Trade-off: SRS is real, differentiating
   signal — just from last season. Zeroing it hands its 12% to the remaining inputs.
2. **Missing-data imputation.** Options: (a) keep neutral-50; (b) renormalize weights over
   present inputs only; (c) impute a below-average baseline for teams with no ratings.
   Trade-off: (b) is statistically cleanest but makes composites depend on how much data a
   team happens to have; (c) is a modelling judgement needing calibration.
3. **FCS teams in the composite.** Given §5.2, an FCS squad receives placeholder points in
   several inputs at once. Options: exclude FCS from the composite board entirely, or
   enable a real FCS rating source (the `fcs_rating` slot cannot do it — §5.5).
4. **`fpi_win_prob` naming and treatment** — rename to reflect what it is, or re-fit the
   scaling (1.5) against real win rates.
5. **Rebuild `D1_COMPOSITE_PROPOSAL.md`** as the standing home for weight proposals, so the
   next change has a documented vehicle.
6. **The efficiency bucket is 37%** — the single largest lever, and the one with the most
   internal structure (5 sub-metrics). If any re-weighting happens, this is where the
   sensitivity analysis should start.

---

## 8. How a change would actually be made

1. Re-fit on real graded results (not on opinion).
2. Present before/after with numbers, and get Jeff's sign-off (CFO doctrine).
3. Edit `COMPOSITE_CONFIG`, which changes `model_version`.
4. Re-run the suite: `selftest_composite_config.py` (total = 1.0), plus predictions and
   rankings suites.
5. Re-validate the downstream curves (margin per composite point, total slope, win-prob
   logistic, win-total scale 3.0) — a weight change moves all of them.
6. Deploy with the standard build-stamp + post-deploy smoke checks.

---

*Findings above are measured from the code and live CFBD responses as of 2026-09-22.
Anything that could not be verified is labelled as an inference rather than stated as fact.*