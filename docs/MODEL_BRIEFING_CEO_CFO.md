# CFB Predictive Model — Complete Findings Briefing

**For:** Jeff, CEO, CFO — a working document for a long conversation
**Date:** 2026-09-22 · **Author:** CTO
**Scope:** everything established about the predictive model, its inputs, its weights, its
measured performance, the SRS data failure, and the CFBD API surface.

**Nothing in this document has been changed.** Every proposal is an option, not an action.
Weight and calibration changes require Jeff's sign-off (CFO doctrine).

**Reproduce any number here:**

```bash
python scripts/probe_input_vintages.py     # is each input current, or a season old?
python scripts/probe_api_surface.py        # endpoint transport, ETag, sizes, quota
python scripts/selftest_composite_config.py
curl -s https://skeezcfb-rankings.com/api/record   # live graded record
```

---

# PART 0 — Bottom line

1. **The model is a good straight-up picker and a good TOTALS model. It is a losing SPREAD
   model.** Season to date (260 graded games): straight-up **78.5%**, totals **55.8%**,
   spreads **39.5%**.
2. **The spread side is not marginal — it is decisively unprofitable.** Break-even at −110
   is **52.4%**; the model is at **39.5%**. The model's own "star" spread tier is *no
   better* (39.4%) — confidence carries no signal on spreads.
3. **The model takes the underdog on 71% of spread picks and wins 38% of them** — but the
   obvious explanation for that (margins too tight) was **tested and refuted** (Part 10).
   In FBS-vs-FBS games the model's margins are *larger* than the market's (**+1.51 pts**)
   and it takes the dog only 56% of the time. The dog-heavy behaviour comes from games
   against FCS/D2/D3 opponents, where it takes the dog **94%** of the time. Spreads lose in
   both classes (41.6% FBS, 36.9% non-FBS), but the *mechanism* is not the one it looked
   like, and the fix is therefore not where it looked like either.
4. **One composite input is a full season old.** SRS (12% of the composite) is served from
   the **2025** season because CFBD has not published 2026 SRS. It was invisible — the page
   displayed it as current. It is now labelled; the **weight is untouched**.
5. **13 of 14 inputs are current.** SRS is the only stale one, so this is a single-input
   problem, not a systemic one.
6. **Missing data silently becomes "league average", which is real points.** For 420 of 685
   teams SRS contributes a flat 6.0 points — a placeholder, not an assessment.
7. **The model is validated point-in-time** (no future leakage) on 131 FBS matchups, and
   the totals edge replicates out-of-sample: backtest 62.8% vs live 55.8%.

**The decisions requested are in Part 9.** The shortest version: *do we fix the margin
curve before touching weights, and do we stop publishing a spread pick the record shows
loses money?*

---

# PART 1 — What the model is

```
14 CFBD endpoints
  → normalized inputs (each 0–100)
  → COMPOSITE (weighted sum, 0–100)
  → projected score per team
  → margin / total / win probability
  → picks (straight-up, spread, total) + season win totals
```

Everything below is driven by one number per team: the **composite**. If the composite
moves, every output moves.

---

# PART 2 — The weights

Single source of truth: `COMPOSITE_CONFIG`. Hashed into `model_version` = **`ca2f071afda`**.
`_assert_weights_sum()` raises if the enabled weights do not total 1.0.

| weight | input | what it represents |
|---|---|---|
| **0.37** | Efficiency (5 sub-metrics) | on-field, per-play performance |
| 0.18 | SP+ | opponent-adjusted efficiency rating |
| 0.15 | FPI | ESPN FPI score, rescaled |
| **0.12** | **SRS** | Simple Rating System — **currently 2025 data** |
| 0.10 | Talent prior | 247 composite + recruiting + returning production |
| 0.08 | Elo | CFBD Elo |
| 0.00 | `fcs_rating` | deliberately inert (a poll RANK, not a score — §6.4) |
| 0.00 | `massey` | deliberately inert |

**Change control.** The code states that changing weights "changes what the model says, so
per CFO doctrine and the checklist's own instruction it needs **JEFF'S APPROVAL**." A weight
change also changes `model_version`, which means historical predictions stop being directly
comparable — so any change should be accompanied by a re-fit and a re-validation of the
downstream curves in Part 4.

---

# PART 3 — How inputs are normalized

`clamp01` = `max(0, min(100, x))`. Every input lands on 0–100 so weights are comparable.

```
sp_norm     = clamp01((sp_plus + 40) / 80 × 100)
fpi_norm    = clamp01(fpi_win_prob)          ← derived, see §6.3
srs_norm    = clamp01((srs + 25) / 50 × 100)
elo_norm    = clamp01((elo − 1500) / 300 × 100 + 50)

talent_norm = program_talent × 0.60 + ret_norm × 0.40
    program_talent   = talent_comp_norm × 0.70 + rec_norm × 0.30
    talent_comp_norm = clamp01((247 talent − 250) / 750 × 100)
    rec_norm         = clamp01((130 − recruiting rank) / 129 × 100)
    ret_norm         = clamp01(pct_ppa_returning or 50)

eff_norm    = sr × 0.28 + epa × 0.24 + ppo × 0.16 + trench × 0.16 + ppd × 0.16
```

### The efficiency bucket (37% — the largest single lever)

| sub-metric | internal | of whole composite | CFBD source |
|---|---|---|---|
| Net success rate | 0.28 | ~10.4% | `stats/season` → `successRate` |
| Net EPA/PPA per play | 0.24 | ~8.9% | `ppa/teams` → `overall` |
| Finishing drives (PPO) | 0.16 | ~5.9% | `stats/season/advanced` → `pointsPerOpportunity` |
| Trench dominance | 0.16 | ~5.9% | `stats/season/advanced` → `lineYards`, `stuffRate` |
| True points per possession | 0.16 | ~5.9% | computed from `drives` |

Each sub-metric is a **differential** (our offense − their defense) centered so a
league-average team lands at 50.

---

# PART 4 — What the composite produces

```
total  = 51 + (avg_composite − 50) × 0.10        # elite games trend slightly higher
margin = (comp_home − comp_away) × 1.0           # 1.0 pt margin per composite point
       + 2.5 if home (0 on a neutral site)
       + net injury adjustment
home_score = (total + margin) / 2   away_score = (total − margin) / 2
win prob   = 1 / (1 + e^(−0.08 × (composite − 50)))
season win totals: logistic scale 3.0 (fitted vs 138 sportsbook win-total lines)
FCS branch : base_score = max(6.0, 27.0 + (fcs_composite − 50) × 0.55)
```

**Three adjustments are applied AFTER the margin is computed.** Anyone auditing a projected
margin by hand will see residuals that are NOT bugs:

- **net injury** — `(home_adj − away_adj)`; a starting QB out is −10.0 pts (Jeff's rule)
- **wind** — total only, when sustained wind ≥ 15 mph
- **underdog floor** — when the composite gap exceeds 30, the weak side's share of the
  total is capped (~10%), which rewrites both scores and therefore moves `differential`
  after the fact

**ATS rule (Jeff's):** if the model margin is UNDER the spread, the underdog is the ATS side.

**FCS blowout boost:** `FCS_BLOWOUT_BOOST = 0.0` — a deliberate named zero. It existed when
the FCS prior was a flat 16; once the composite carried the FBS/FCS gap it double-counted
(predicted 51.5 against a measured 37.2).

---

# PART 5 — Measured performance (this is the CFO's section)

## 5.1 Live record, 2026 season to date (through week 3)

Source: `https://skeezcfb-rankings.com/api/record`, 263 picks, 260 graded.

| market | record | rate | verdict |
|---|---|---|---|
| Straight up | **204–56** | **78.5%** | strong |
| **Spread (ATS)** | **102–156–2** | **39.5%** | **losing — break-even is 52.4%** |
| Totals (O/U) | **145–115** | **55.8%** | profitable |
| "Stars" totals (highest conviction) | 44–30 | **59.5%** | profitable |
| **"Stars" spreads (highest conviction)** | 93–143 | **39.4%** | **losing — no better than taking every pick** |

**The conviction tiers do not work on spreads.** The model's own highest-conviction spread
picks (39.4%) perform the same as its whole board (39.5%). On totals, conviction *does*
work (59.5% vs 55.8%).

## 5.2 Why the spread side loses — the underdog bias

Every graded spread pick, split by which side the model took:

```
model took the UNDERDOG : 70–114 = 38.0%   (71% of all spread picks)
model took the FAVORITE : 32– 42 = 43.2%
```

The model selects the underdog on **71%** of spread picks and wins **38%** of them. A model
taking dogs that often should win near 50% if its margins were centred; winning 38% means
its projected margins are systematically **too tight** — it fails to make favourites big
enough favourites relative to the market.

By size of the line — no bucket is profitable:

```
 0– 7 pts: 18–28 = 39.1%
 7–14 pts: 15–28 = 34.9%
14–21 pts: 15–17 = 46.9%
21–28 pts: 18–28 = 39.1%
28+  pts: 36–55 = 39.6%
```

## 5.3 Independent backtest (point-in-time, no future leakage)

`data/backtest_summary.json` — 360 games evaluated, **131 FBS matchups** (FCS excluded),
week 1 from a frozen preseason baseline, later weeks only from completed games, closing
lines frozen at kickoff (DraftKings primary, Bovada secondary).

```
SPREADS                                     TOTALS
all FBS picks    56–66–2  45.9%             all totals        76–45–0  62.8%
≥2.0 pt          44–54–2  44.9%             ≥4.0 pt (3-star)  27–21–0  56.2%
≥3.5 pt          31–40–1  43.7%             ≥7.0 pt (5-star)   4– 8–0  33.3%
≥7.0 pt          17–20–1  45.9%
```

Walk-forward by week (spreads): week 1 **47.9%**, week 2 **42.9%**, week 3 **43.8%** —
consistently below break-even, so this is not one bad week.

## 5.4 Where the model demonstrably works: totals

The totals signal replicates across independent samples:

- backtest: **62.8%** on all FBS totals (121 decided)
- live: **55.8%** on all totals (260 graded)
- live highest-conviction: **59.5%** (74 graded)
- CLV-tracked 3-star totals (≥4.0 edge): **60.3%**, **+12.1 units ROI**, vs 52.38% break-even

The **5-star totals tier (≥7 pt edge) is the exception — 33.3% on only 12 games.** Small
sample, but worth noting: more conviction did not help there.

## 5.5 Statistical honesty about sample sizes

- 260 graded games on totals/ATS; 258 ATS decisions.
- A 39.5% ATS rate over 258 games is far outside the range explainable by variance — it is
  a real, structural result.
- The 12-game 5-star totals tier and the 74-game stars tier are small; treat their exact
  rates as indicative, not settled.
- All of the above is one season, weeks 1–3, and week-1 lines are the least informative.

---

# PART 6 — The SRS failure (the specific defect)

## 6.1 What is happening

```
CFBD /ratings/srs?year=2026  →  0 rows      (2025 → 266 rows)
```

`app.py` deliberately falls back to the previous season when the current season is empty
(the same pattern covers `elo` and `talent`). Consequence: **every SRS value the site
serves is last season's**, at **12% composite weight**.

Verified per team against CFBD's 2025 payload — exact matches, 10/10 sampled:

```
Georgia 16 · Ohio State 26.6 · Notre Dame 24.1 · Indiana 27.3 · Alabama 12.7 · Texas 12.6
```

## 6.2 How much of the composite is affected

The weight is 12%, but the **points** contributed scale with the normalized value, so the
real share varies by team. Measured across the live top 25:

```
SRS points contributed : 6.60 – 12.00
share of composite     : 8.5% – 14.9%   (mean 11.6%)
ranked teams missing SRS: 0 of 25
```

**So yes — the entire 12% is last season's data, for every team ranked.**

## 6.3 Missing data is not neutral — it is "league average", i.e. real points

Almost every gap in the model is imputed at 50 and then weighted, so a placeholder becomes a
genuine number of points:

| missing | imputed | effective points |
|---|---|---|
| SRS | norm 50 | **6.0** |
| EPA | norm 50 | 8.9 |
| success rate / PPO / trench / PPD | norm 50 | 8.9 / 5.9 / 5.9 / 5.9 |
| returning production | norm 50 | 2.0 |

**420 of the 685 teams** in the analytics set have no SRS at all (FCS and lower divisions).
For them SRS contributes a flat **6.0** — a constant, not a judgement — while their FBS
opponents can receive up to **12.0**. That is a systematic ~6-point composite gap in
FBS-vs-FCS games originating from a placeholder. (This is the "6 for an FCS school".)

## 6.4 Why this matters for the spread problem specifically

The composite feeds the margin directly:

```
margin = (comp_home − comp_away) × 1.0 + 2.5 HFA
```

A stale or placeholder input on either side therefore moves the spread — and the spread is
exactly the market that is losing at 39.5%. **This is a plausible contributor, not a proven
cause.** The 2025 SRS values are real and differentiating (they rank teams sensibly), so
they may be *less* harmful than a placeholder — but they describe a different season's
teams, and the model cannot tell.

## 6.5 What has been done (no model change)

- The analytics page now **labels the vintage** (`SRS (2025)`) and the SRS tab states the
  fallback and the 12% weight.
- The API exposes the fact mechanically: `input_vintages: {"srs": 2025}` in `/api/rankings`,
  plus per-team `srs_source_year`.
- An hourly, window-bounded probe watches CFBD's release window with an **SRS-2026
  watcher** — silent until 2026 SRS appears, then it reports. Tested both ways.
- **The weight has not moved.** Neither has the fallback.

---

# PART 7 — Other findings

## 7.1 Only one input is stale
Measured across all 14 fallback-capable endpoints for 2026: **13 current, SRS stale.**
Everything else — SP+, FPI, Elo, talent, recruiting, season stats, advanced stats, PPA,
drives, returning, roster, records, teams — is current-season data.

## 7.2 `fpi_win_prob` is not a win probability
CFBD's `/ratings/fpi` provides an `fpi` score and a `ranking`; it publishes **no** win
probability. We derive `fpi_win_prob = clamp(0,100, 50 + fpi_score × 1.5)` and weight it at
**15%**. So 15% of the composite is a linear rescale of the FPI score under a name that
implies more than it is. The name is inherited and the field is still called that in the API.

## 7.3 The `fcs_rating` slot is inert — and that is correct
It holds an FCS **poll rank (1–25)**, not a strength score. Weighting it would map rank 25 as
stronger than rank 1 — an **inverted** signal. Keeping it at 0.0 is right; the correct lever
for FCS opponents is the missing-data prior.

## 7.4 The FCS prior is fitted, not a constant

```
FCS_COMPOSITE_RANKED   = 33.0   (Massey rank ≤ 25)
FCS_COMPOSITE_UNRANKED = 15.0
FCS_COMPOSITE_FALLBACK = 19.0   (no rating at all)
```

Two different datasets, and conflating them is a mistake worth naming:

- **848 real FBS-vs-FCS games** set the *targets*: FBS wins by **31.9** vs unranked,
  **14.2** vs ranked.
- **103 completed games** are where the *error* was measured: MAE **21.5 → 13.6 pts**.

The old flat `16.0` is gone. A stale code comment still described it; that comment has been
corrected (comment only — no behaviour change).

## 7.5 A single age threshold cannot police these inputs
SRS is *legitimately* a season old until CFBD publishes. Any freshness gate built on one age
threshold would fire on SRS forever — and then get ignored or disabled. Any prediction-
freshness rule must be **cadence-aware per input**. The payload now carries
`input_vintages` so a gate can read the vintage rather than infer it.

## 7.6 The referenced proposal document does not exist
`app.py` points at `D1_COMPOSITE_PROPOSAL.md` as the home of the weight proposal. **That file
is not in the repository.** The decision vehicle the code claims to rely on is missing —
worth recreating as part of this conversation.

## 7.7 The ratings release window drives the pull schedule
CFBD publishes ratings at an **unpredictable time between Sunday night and Wednesday**. The
9pm PT pull schedule exists to **catch the release**, not because values change every few
hours. An age-based check cannot detect a release: data can be two hours old and still be
missing a release that landed after our last pull. Release detection and staleness are two
different questions, and the system now addresses both.

---

# PART 8 — The CFBD API map

## 8.1 Basics

| | |
|---|---|
| Base URL | `https://api.collegefootballdata.com` |
| Auth | `Authorization: Bearer <key>` (repo `.env`) |
| Quota signal | response header `X-CallLimit-Remaining` |
| Observed remaining | ~25,750 of a ~30,000 allowance |
| Our usage | negligible — the constraint is correctness, not quota |

## 8.2 Measured surface (2026-09-22, `year=2026`)

`304cost` = calls consumed by a `304` (measured from the quota header).

| Endpoint | Params | Rows | Bytes | ETag | 304? | 304cost |
|---|---|---|---|---|---|---|
| `teams` | — | 682 | 959,950 | yes | yes | 1 |
| `ratings/sp` | — | 139 | 72,267 | yes | yes | 1 |
| `ratings/elo` | — | 138 | 9,543 | yes | yes | 1 |
| `ratings/fpi` | — | 138 | 42,763 | yes | yes | 1 |
| `ratings/srs` | — | **0** | 2 | yes | yes | 1 |
| `records` | — | 682 | 356,643 | yes | yes | 1 |
| `recruiting/teams` | — | 221 | 13,280 | yes | yes | 1 |
| `talent` | — | 138 | 6,832 | yes | yes | 1 |
| `stats/season` | — | 8,487 | 896,086 | yes | yes | 1 |
| `stats/season/advanced` | — | 138 | 337,440 | yes | yes | 1 |
| `ppa/teams` | — | 138 | 54,694 | yes | yes | 1 |
| `ppa/players/season` | — | 4,581 | 1,862,963 | yes | yes | 1 |
| `player/returning` | — | 136 | 43,892 | yes | yes | 1 |
| `games` | — | 3,679 | **2,756,460** | yes | **NO** | — |
| `lines` | `week` | 71 | 43,453 | yes | yes | 1 |
| `drives` | `week`,`team` | 0 | 2 | yes | yes | 1 |
| `roster` | `team` | 128 | 38,067 | yes | yes | 1 |
| `games/weather` | `week`,`team` | 1 | 436 | yes | yes | 1 |

**18/18 endpoints return an ETag; 17/18 honour it.** The exception is the largest payload
we pull.

## 8.3 Conditional polling — what it does and does not buy

`304` works on 17 of 18 endpoints and returns **0 bytes**. **It still costs one call.** So
conditional polling saves the body, the parse, the recompute and the database write — it
does **not** save quota. The conditional-200 body is byte-identical to a plain GET
(sha256-verified), so it returns the same data, not a truncated variant.

## 8.4 `games` cannot use ETags — its row order is unstable

```
call 1  ETag W/"2a0f6c-wLnd6l+3..."   sha256 498f1485eca0092d
call 2  ETag W/"2a0f6c-kk16c/ki..."   sha256 735f0f2e26e906fc
call 3  ETag W/"2a0f6c-bR7mCz1F..."   sha256 38b57c7540f4784e
same id SET: True   same id ORDER: False
content identical once sorted by id: True
canonical hash (sorted by id, sorted keys): 08de10a063af3b56 (stable across calls)
```

Never use `/games`' ETag as a change signal, and never diff it positionally — that reported
**3,238 false "changed"** rows when nothing had changed. Canonicalise (sort by id, hash)
before comparing.

## 8.5 Known traps

1. **A `304` still costs a call.** Budget it as a call.
2. **`/games` ETag is volatile by design.** Canonicalise.
3. **`ratings/srs?year=2026` returns an empty array**, and the app silently falls back to the
   previous season — the defect in Part 6. Any per-input freshness gate must be
   cadence-aware, not age-threshold-based.
4. **Empty results are not errors.** `drives` (pre-game) and `ratings/srs` return `[]` with
   HTTP 200. Treat 200 + empty as "no data yet", and always check whether a caller has
   silently switched to a fallback.
5. **Param-dependent sizes.** Some endpoints need `week`/`team`; omitting them returns 200
   with an empty array — a silent wrong answer rather than an error.
6. **Big payloads:** `games` 2.76 MB, `ppa/players/season` 1.86 MB, `teams` 960 KB,
   `stats/season` 896 KB. A full ingest is ~7 MB.

---

# PART 9 — Diagnosis and decision agenda

## 9.1 The central diagnosis — TESTED, and it changed (see Part 10)

The first hypothesis was that the margin curve under-separates teams, so the model takes
underdogs and loses. **That was tested and refuted.** In FBS-vs-FBS games the model's
average absolute margin is **17.47 pts versus the market's 15.96** — it is *more* aggressive
than the book, not less, and it takes the dog only 56% of the time.

What the data actually shows:

- **Spreads lose in BOTH classes** — 41.6% FBS-vs-FBS, 36.9% involving FCS/D2/D3. So this is
  not purely an FCS problem.
- **The dog-heavy behaviour is an FCS/D2/D3 phenomenon** — 94% of picks in those games are
  underdogs (versus 56% in FBS games).
- **The totals edge is FBS-vs-FBS only** — 59.0% there, 51.0% involving non-FBS opponents.
- **Removing SRS does not fix spreads** (Part 10): it moves the FBS ATS rate from 48.3% to
  45.9% on identical data. SRS is not the culprit, and removing it would make the model
  *more* dog-heavy, not less.

The honest conclusion: the spread projection has a genuine, structural problem that SRS
does not explain; the strongest immediate wins are (a) scoping the totals product to
FBS-vs-FBS games where the edge is real, and (b) treating spread picks as unproven until the
margin model is re-fitted on graded results.

## 9.2 Decision list

**A. The spread market (highest priority).**
The published spread picks lose money at 39.5%, and the conviction tier does not help.
Options:
1. Re-fit the margin curve against graded results *before* touching weights.
2. Stop publishing spread picks until the curve is re-fitted (totals and straight-up remain).
3. Keep publishing with an explicit, visible record — the record is already public.
4. Cap or remove the underdog floor, which interacts directly with the 71% underdog rate.

**B. SRS (12%).**
Options: leave the weight and rely on the vintage label; reduce it until 2026 publishes;
zero it and re-normalise the remaining weights; or treat it as a prior rather than a
current-season signal. Note SRS is real, differentiating signal — just from last season.

**C. Missing-data imputation.**
Neutral-50 makes placeholders into real points. Options: keep it; re-normalise weights over
only the inputs a team actually has; or impute a below-average baseline for teams with no
ratings (calibration required).

**D. FCS teams in an FBS composite.**
420 of 685 teams carry a flat SRS placeholder. Options: exclude FCS from the composite
board; or give them a real rating source (the `fcs_rating` slot cannot do this — §7.3).

**E. `fpi_win_prob` naming and scaling.**
Rename to reflect what it is, and/or re-fit the 1.5 scaling against real win rates.

**F. Recreate `D1_COMPOSITE_PROPOSAL.md`.**
The code already points at it as the weight-proposal home. It should exist.

**G. Conviction tiers.**
Totals conviction works (59.5% vs 55.8%); spread conviction does not (39.4% vs 39.5%).
The tiering logic itself deserves review on the spread side.

## 9.3 How a change would be made (procedure)

1. Re-fit on graded results, not on opinion.
2. Present before/after with numbers; obtain Jeff's sign-off (CFO doctrine).
3. Edit `COMPOSITE_CONFIG` (changes `model_version`).
4. Run the suite: `selftest_composite_config.py` (weights = 1.0), predictions, rankings.
5. Re-validate downstream curves — margin per composite point, total slope, win-prob
   logistic, win-total scale.
6. Deploy with build-stamp + post-deploy smoke checks on every page.

---

# PART 10 — The SRS-zeroed experiment (and the opponent-class split)

Run 2026-09-23, at Jeff's direction. Two questions: *is the stale SRS input causing the
spread losses?* and *where does the model actually lose?*

## 10.1 Method — the same games, two weightings

The existing point-in-time harness was run twice over **identical reconstructed data**
(409 games, **155 FBS matchups**; week 1 from the frozen preseason baseline, later weeks
only from completed prior games; closing lines frozen at kickoff). Only the weights differed:

| | sp_plus | fpi | srs | elo | talent | efficiency | hash |
|---|---|---|---|---|---|---|---|
| baseline | 0.18 | 0.15 | **0.12** | 0.08 | 0.10 | 0.37 | `ca2f071afda` |
| zero-srs | 0.204545 | 0.170455 | **0.0** | 0.090909 | 0.113636 | 0.420455 | `c0229dfa0f4` |

The 12% freed by removing SRS was **re-normalised proportionally** across the remaining
inputs (so they still total 1.0 — anything else would be a miscalibration, not a fair test).
No code change to the model: the harness already honours `COMPOSITE_WEIGHTS_JSON`, and the
hash changes automatically, which is what makes the experiment auditable.

## 10.2 Result — SRS is NOT the cause of the spread losses

```
SPREADS (ATS)                          baseline          zero-srs      delta
  all FBS picks          69-74-4  48.3%   67-79-4  45.9%     -2.4
  >= 2.0 pt              56-61-4  47.9%   51-58-4  46.8%     -1.1
  >= 3.5 pt              39-46-3  45.9%   41-47-3  46.6%     +0.7
  >= 7.0 pt              23-24-1  48.9%   23-22-1  51.1%     +2.2

TOTALS (O/U)                           baseline          zero-srs      delta
  all totals             86-54-0  61.4%   86-55-0  61.0%     -0.4
  >= 4.0 pt              34-23-0  59.6%   33-22-0  60.0%     +0.4
  >= 7.0 pt               5- 8-0  38.5%    8- 8-0  50.0%    +11.5  (16 games, noise)

DIAGNOSTICS                            baseline          zero-srs
  took the underdog       26-31  45.6%   (38% of picks)   33-41  44.6%   (49% of picks)
  took the favourite      45-49  47.9%                   35-42  45.5%
  mean |model margin|           17.47                     16.67
  mean |book line|              15.96                     15.96
  vs book: MAE / bias     5.58 / -0.20                5.68 / -1.02
```

**Conclusion: removing SRS does not help the spread side — it makes it slightly worse**
(48.3% → 45.9%), and it is neutral on totals (61.4% → 61.0%). Two further things fall out:

- **SRS is currently making the model more willing to back favourites.** Without it the
  underdog share rises from 38% to 49% and the average margin shrinks by 0.8 pts. So the
  season-old input is contributing separation, not diluting it.
- **Removing SRS costs nothing on totals**, which is the one market that works. If the
  weight is reduced for honesty reasons, the totals edge does not pay for it.

## 10.3 The compression hypothesis, refuted

The earlier explanation for the 71% underdog rate was that the model's margins were too
tight. **Measured, that is false in FBS games**: model 17.47 pts average absolute margin
versus the book's 15.96 — the model is *more* aggressive. (It is slightly tight-tailed: MAE
5.58 pts, bias −0.20.)

## 10.4 Where the losses actually live — live record split by opponent class

Splitting the live 2026 record by opponent classification (CFBD `/teams`:
138 FBS, 128 FCS, 170 D-II, 246 D-III):

```
                          FBS vs FBS        involves FCS/D2/D3
spreads (ATS)         64-90  = 41.6%      38-65  = 36.9%
totals (O/U)          92-64  = 59.0%      53-51  = 51.0%
underdog share        56% of picks        94% of picks
underdogs themselves  36-50  = 41.9%      34-63  = 35.1%
                       (154 graded)       (103 graded)
```

Three conclusions:

1. **The totals edge is an FBS-vs-FBS phenomenon** — 59.0% there, 51.0% against non-FBS
   opponents. Non-FBS games dilute a real edge toward a coin flip. This is the single most
   actionable finding in the document.
2. **Spread losses are real in both classes** (41.6% / 36.9%), so this is not merely an FCS
   problem — but non-FBS games are worse and far more dog-heavy.
3. **The 71% underdog rate is driven by non-FBS games (94% dogs).** In FBS games the model
   takes the dog 56% of the time, which is unremarkable.

## 10.5 What this changes about the decision list

- **Do not expect an SRS fix to repair spreads.** It was the leading hypothesis and it is
  now a testable no. Any weight change should be justified on totals/FBS grounds instead.
- **Scope the totals product to FBS-vs-FBS games.** That is where the demonstrated edge is.
- **The spread problem is structural and still unexplained.** It needs a re-fit of the
  margin model against graded results, not a weight tweak. The 1.0 pt-per-composite-point
  slope is the prime suspect and is untested.
- **Caveat, stated plainly:** the backtest arms are not a perfect proxy for the live pick
  pipeline — the live path also applies injury adjustments, wind, the underdog floor and
  live line overlays, and it runs on mid-season composites rather than a frozen preseason
  baseline. That is why the backtest's FBS ATS rate (48.3%) is better than the live
  FBS-vs-FBS record (41.6%). The A/B comparison between the two weightings is sound because
  both arms share the same harness; the absolute levels should not be read as the live rate.

## 10.6 Reproduce it

```bash
# baseline arm (weights default; hash ca2f071afda)
python scripts/run_model_backtest.py --label baseline \
    --out-summary out-bt-base.json --out-fixture out-bt-base-fix.json
# zero-srs arm (weights honoured from the environment; hash becomes c0229dfa0f4)
COMPOSITE_WEIGHTS_JSON='{"sp_plus":0.20454545,"fpi":0.17045455,"srs":0.0,"elo":0.09090909,"talent":0.11363636,"efficiency":0.42045455}' \
python scripts/run_model_backtest.py --label zero-srs \
    --out-summary out-bt-zero.json --out-fixture out-bt-zero-fix.json
python scripts/compare_backtest_arms.py out-bt-base.json out-bt-base-fix.json \
    out-bt-zero.json out-bt-zero-fix.json --labels baseline zero-srs
```

`--out-summary`/`--out-fixture` were added so an experiment can never overwrite the
published `data/backtest_summary.json` / `data/backtest_fixture_2026.json` (verified
byte-identical after both runs).

---

# Appendix A — Glossary

| Term | Meaning |
|---|---|
| Composite | 0–100 weighted strength score per team; the model's core number |
| ATS | Against the spread |
| Closing line | The final market line at kickoff; the benchmark for grading |
| CLV | Closing line value — did the price beat the closing number |
| Point-in-time | Backtest uses only data available before each game (no leakage) |
| MAE | Mean absolute error |
| Vintage | Which season a value actually came from |
| 304 | HTTP "not modified"; costs a call but returns no body |

# Appendix B — Provenance

- Weights/normalizations: read from `COMPOSITE_CONFIG` and `_enrich_with_composite()`.
- Live record: `https://skeezcfb-rankings.com/api/record` (2026-09-23).
- Backtest: `data/backtest_summary.json` (point-in-time, FCS excluded).
- CLV: `data/clv_3star_totals_track.json`.
- Input freshness: `scripts/probe_input_vintages.py`.
- Transport: `scripts/probe_api_surface.py` → `data/api_surface_map.json`.
- Field-level map: `docs/CFBD_COMPOSITE_INPUTS.md`.
- Weights review: `docs/MODEL_WEIGHTS_REVIEW.md`.

*Every number here was measured from the code or the live API on 2026-09-22/23. Hypotheses
are labelled as hypotheses. Nothing in this document has been applied to the model.*