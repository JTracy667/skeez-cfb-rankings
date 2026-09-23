# CFBD API → Composite Inputs Map

**What this is:** every CFBD endpoint that feeds the Skeez composite, the exact field we
read from each, how it is normalized, and what it is worth. `docs/CFBD_API_MAP.md` covers
the *transport* (params, payload sizes, ETags, quota). This covers the *data* — the points
that actually go into the ranking.

**Regenerate the freshness column any time:**

```bash
python scripts/probe_input_vintages.py            # live row counts, current vs fallback
python scripts/probe_api_surface.py               # transport surface (ETag/304/payload)
```

Model identity: weights live in `COMPOSITE_CONFIG` (one source of truth, hashed into
`model_version` = `ca2f071afda`). Normalization lives in `_enrich_with_composite()`.

---

## 1. Measured status — is each input actually CURRENT?

Measured 2026-09-22 against `?year=2026`:

```
input              endpoint                  rows      verdict
SP+                ratings/sp                 139   CURRENT
FPI                ratings/fpi                138   CURRENT
SRS                ratings/srs                  0   STALE -> previous season
Elo                ratings/elo                138   CURRENT
Talent (247)       talent                     138   CURRENT
Recruiting         recruiting/teams           221   CURRENT
Season stats       stats/season              8487   CURRENT
Advanced stats     stats/season/advanced      138   CURRENT
PPA/teams          ppa/teams                  138   CURRENT
Drives             drives                   11109   CURRENT
Player returning   player/returning           136   CURRENT
Roster             roster                   31283   CURRENT
Records            records                    682   CURRENT
Teams              teams                      682   CURRENT
```

**SRS is the only stale input.** Everything else is current-season. That matters: the
problem is one input, not a systemic fallback.

---

## 2. The composite

```
composite = sp_norm*0.18 + fpi_norm*0.15 + srs_norm*0.12 + elo_norm*0.08
          + talent_norm*0.10 + eff_norm*0.37 + fcs_norm*0.00 + 50.0*0.00
```

| weight | input | contributes |
|---|---|---|
| **0.37** | efficiency (5 sub-metrics) | the largest single lever |
| 0.18 | SP+ | |
| 0.15 | FPI | |
| 0.12 | **SRS** | **currently a season old** |
| 0.10 | talent prior | 247 composite + recruiting + returning production |
| 0.08 | Elo | |
| 0.00 | `fcs_rating` | deliberately inert — it is a POLL RANK (1..25), not a strength score; a weight would invert the signal |
| 0.00 | `massey` | deliberately inert |

Weights are **CFO doctrine** and require **Jeff's approval** before they move
(`_assert_weights_sum()` enforces they sum to 1.0).

---

## 3. Input-by-input: endpoint → field → normalization

`clamp01` below means `max(0, min(100, x))` — every input lands on a 0–100 scale so the
weights are comparable.

### 3.1 Anchor ratings

| input | endpoint | CFBD field | normalization | weight |
|---|---|---|---|---|
| SP+ | `ratings/sp` | `rating` | `clamp01((sp + 40) / 80 × 100)` | 0.18 |
| FPI | `ratings/fpi` | `fpi` | see caveat | 0.15 |
| SRS | `ratings/srs` | `rating` | `clamp01((srs + 25) / 50 × 100)` | 0.12 |
| Elo | `ratings/elo` | `elo` | `clamp01((elo − 1500) / 300 × 100 + 50)` | 0.08 |

**FPI caveat — this is not a CFBD win probability.** CFBD's `/ratings/fpi` gives an `fpi`
score and a `ranking`; it does not publish a win probability. We DERIVE it:

```
fpi_win_prob = clamp(0, 100, 50 + fpi_score × 1.5)
fpi_norm     = clamp(0, 100, fpi_win_prob)
```

So the FPI input is a linear rescale of the FPI score, not a modelled win probability.
The field name `fpi_win_prob` overstates what it is. (Marked in the payload as
`fpi_win_prob`; a rename to `fpi_scaled` would be honest but changes the payload shape.)

### 3.2 Talent prior (10%)

Three CFBD sources blended, then normalized:

```
talent  (247 85-man composite, 250..1000)  → talent_comp_norm = clamp01((talent − 250) / 750 × 100)
recruiting/teams  rank                     → rec_norm         = clamp01((130 − rank) / 129 × 100)
program_talent = talent_comp_norm × 0.70 + rec_norm × 0.30      (rec_norm alone if no 247)
returning production (player/returning)    → ret_norm         = clamp01(pct_ppa_returning or 50)
talent_norm    = program_talent × 0.60 + ret_norm × 0.40
```

`pct_ppa_returning` is the share of PPA returning — a real CFBD number when present, and a
**neutral 50 when absent**.

### 3.3 Efficiency bucket (37%) — five sub-metrics

This is the biggest lever in the model, so its internal split matters:

| sub-metric | weight within eff | endpoint | CFBD field(s) | normalization |
|---|---|---|---|---|
| Net success rate | 0.28 | `stats/season` | `successRate` (off & def) | `clamp01((off_sr − def_sr + 0.20) / 0.40 × 100)` |
| Net EPA / PPA per play | 0.24 | `ppa/teams` | `overall` (off & def) | `clamp01((epa − def_epa + 0.40) / 0.80 × 100)` |
| Finishing drives (PPO) | 0.16 | `stats/season/advanced` | `pointsPerOpportunity` | `clamp01((off_ppo − def_ppo + 2.5) / 5.0 × 100)` |
| Trench dominance | 0.16 | `stats/season/advanced` | `lineYards`, `stuffRate` | `ly = clamp01((off_ly − def_ly + 1.5) / 3.0 × 100)`; `st = clamp01((def_st − off_st + 0.15) / 0.30 × 100)`; `trench = ly × 0.6 + st × 0.4` |
| True points per possession | 0.16 | `drives` | derived: points ÷ drives | `clamp01((pts_poss − def_pts_poss + 2.0) / 4.0 × 100)` |

Every sub-metric is a **differential** (our offense minus their defense) centered so that
a league-average team lands at 50. That is why the constants look arbitrary — they are the
league means from the calibration, not magic numbers.

`drive`-derived PPD is computed by us from `/drives` (sum points ÷ sum drives per team),
not read from a field.

### 3.4 Deliberately inert slots

| slot | source | weight | why |
|---|---|---|---|
| `fcs_rating` | FCS Coaches Poll (1..25) | 0.0 | a rank, not a score — weighting it would invert the signal |
| `massey` | Masseyratings scrape | 0.0 | unused |

---

## 4. Missing data: what happens when a field is absent

This is where the composite can mislead, because **almost every gap silently becomes a
neutral 50** — which after weighting is a real number of points, not a blank.

| missing | becomes | effective points | note |
|---|---|---|---|
| SRS (`0` from an empty endpoint) | `srs_norm = 50` | **6.0** | the "6 for an FCS school" — a placeholder, not a rating |
| EPA | `or 0.0` → norm 50 | 8.9 | |
| success rate / PPO / trench / PPD | 50 | 8.9 / 5.9 / 5.9 / 5.9 | |
| returning production | 50 | 2.0 (inside talent) | |
| 247 talent | `rec_norm` alone | — | recruiting still differentiates |
| FCS teams generally | flat prior | — | see §5 |

**Consequence worth stating plainly:** a team with *no data at all* still scores a
respectable composite, because every gap is imputed at "league average". And a team
missing only SRS gets exactly 6.0 from that input while a team with a real SRS can get up
to 12.0 — a systematic ~6-point gap that comes from a placeholder, not from football.

---

## 5. Open items flagged for CFO / Jeff (not changed by the CTO)

1. **SRS is a full season old and carries 12%.** Measured: 0 rows for 2026, 266 for 2025.
   The fallback is deliberate in code. *(Now labelled in the UI rather than silent.)*
2. **Missing-SRS imputation is "league average" (norm 50 → 6.0).** For 420 of 685 teams
   this is a constant, not a signal, and it inflates FCS teams relative to their true
   level — directly relevant to FBS-vs-FCS projections.
3. **`fpi_win_prob` is a rescaled FPI score, not a win probability.** Naming overstates it.
4. **A stale comment in `_enrich_with_composite()`** still says the FCS prior is "a flat
   16.0 for every FCS team regardless of quality" — superseded by the rating-derived prior
   (unranked 15 / poll-ranked 33 / no rating 19). Stale comment, not stale behaviour.
5. **A single age threshold cannot police these inputs.** SRS is *legitimately* a season
   old until CFBD publishes; an age-based freshness gate would fire on it forever. Any
   prediction-freshness gate must be **cadence-aware per input**, and now has
   `input_vintages` in the payload to read.

---

*Generated from the code, not from memory. Row counts are live as of the date stamped in
`data/input_vintages_probe.json`.*