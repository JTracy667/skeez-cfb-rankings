# Multi-Year Backtesting — design, verified data availability, and fidelity findings

Status: **design agreed, data layer not yet built.** Week-1-onward approved by Jeff
2026-09-23; retroactive *preseason* snapshots explicitly deferred ("if we want to start
testing our own preseason stuff later on we can look at preseason inputs").

Owner: CTO. Model/weight decisions remain Jeff's (CFO doctrine) — this document is about
how to *measure*, not what to change.

---

## 1. The decision, and what it means concretely

Test **week 1 onward** for each season. Since no true preseason snapshot exists for past
seasons (we froze 2026's ourselves, and a preseason rating set cannot be captured
retroactively), week 1 of season *Y* uses a **prior-season prior**:

| input | source for season Y's week-1 prior | leak status |
|---|---|---|
| `sp_plus`, `sp_offense`, `sp_defense`, `sp_rank` | season **Y−1** final ratings | past data — clean |
| `elo`, `fpi`/`fpi_win_prob` | season **Y−1** final ratings | past data — clean |
| `srs` | season **Y−1** final ratings | past data — clean |
| `talent_score`, `recruiting_*` | season **Y** offseason values | known before kickoff — clean |
| `pct_ppa_returning` | season **Y** offseason value | known before kickoff — clean |
| efficiency fields | season **Y−1** final season stats | past data — clean |

This is leak-free by construction and it is **not a hack**: it is exactly the relationship
the live model already has with SRS, which is served from the previous season today
(`input_vintages: {"srs": 2025}`). The multi-year rig will therefore reproduce, at week 1,
the same kind of prior the live model actually operates on.

**Explicitly out of scope for now:** a genuine preseason rating set for 2021–2025. Nobody
can obtain it after the fact, so "testing our own preseason inputs" later means capturing
*future* preseasons going forward, not reconstructing past ones.

---

## 2. Verified data availability (probed live 2026-09-23)

Every input needed for the prior-season baseline exists for **2019–2025**:

```
year   sp+    elo    fpi    srs   talent  recruiting
2019   131    131    130    130     231       223
2020   131    127    127     77*    219       206
2021   131    130    130    130     224       191
2022   132    131    131    261     233       184
2023   134    133    133    261     238       177
2024   135    134    134    265     134       194
2025   137    136    136    266     134       232
```
`*` 2020 is the COVID season — SRS is thin and schedules are non-comparable. Treat any
2020 result as unrepresentative rather than as evidence.

Also verified: `/stats/season/advanced?year=Y` (134 teams in 2024) providing offense+defense
`successRate`, `ppa`, `pointsPerOpportunity`, `lineYards`, `stuffRate`, `havoc`,
`explosiveness`; `/stats/game/advanced?year=Y&week=W` (224 rows in 2024 wk 8);
`/lines?year=Y` (1,573 rows in 2024).

**In-D1 coverage (CFB history DB, `c3ec3149-…`):** `games` 21,204 rows (2021–2026),
`stat_observations` 57,854 rows with a `week` column, `closing_lines`, `teams`.

### The trap that would have manufactured fake results

Every rating row in `stat_observations` sits at **`week 0` — a full-season aggregate**
(`sp_plus`, `talent_rating`, `sp_plus_rk`, and the counting stats all report `weeks 0-0`;
only `fcs_rating` is weekly, weeks 1–14). Backtesting season *Y* with season *Y*'s
aggregate ratings is **leakage**: the model would already know the season it is predicting.
So the history DB is the *index*, not the *inputs* — per-week inputs must come from CFBD's
weekly historical endpoints, which is what §1 specifies.

---

## 3. The exact field spec the baseline must satisfy

The composite reads these keys from a team record (`app.py` project path). This list is the
contract — a missing key silently becomes a neutral-50 imputation, not an error.

**Ratings (weights 0.18 / 0.15 / 0.12 / 0.08):** `sp_plus`, `fpi`(→`fpi_win_prob`),
`srs`, `elo`
**Talent (0.10):** `talent_score`, plus recruiting inputs, plus `pct_ppa_returning`
**Efficiency bucket (0.37 total, sub-weights 0.28/0.24/0.16/0.16/0.16):**

| sub-metric | keys read |
|---|---|
| net success rate | `off_success_rate`, `def_success_rate` |
| net EPA/PPA per play | `epa_play`, `def_epa_play` |
| finishing drives | `off_ppo`, `def_ppo` |
| trench dominance | `off_line_yards`, `def_line_yards`, `off_stuff_rate`, `def_stuff_rate` |
| true points per possession | `pts_per_poss`, `def_pts_per_poss` |

CFBD mapping for the historical baseline: `successRate`→`*_success_rate`,
`ppa`→`epa_play`/`def_epa_play`, `pointsPerOpportunity`→`*_ppo`, `lineYards`→`*_line_yards`,
`stuffRate`→`*_stuff_rate`. `pts_per_poss`/`def_pts_per_poss` do **not** appear in CFBD's
advanced payload under that name — the 2026 file carries them from the live pipeline, so
prior seasons need either an alternative source or an honest neutral-50.

---

## 4. Fidelity findings — the backtest has been running with ~half the efficiency signal

Verified empirically (live `/api/analytics` vs the backtest's `data/cfbd_analytics_preseason.json`,
and the reconstruction function itself), not read off the source:

**The 37% efficiency bucket is only ~52% alive inside the backtest.** Population of its five
sub-metrics, 685 live teams vs the 687-team backtest baseline:

| sub-metric (sub-weight) | keys | live non-null | backtest baseline non-null |
|---|---|---|---|
| net success rate (0.28) | `off/def_success_rate` | 138/685 | **0** — but refreshed in-season from weekly stats |
| net EPA/PPA (0.24) | `epa_play`, `def_epa_play` | 138/685 | **0** — but refreshed in-season from weekly stats |
| finishing drives (0.16) | `off_ppo`, `def_ppo` | 138/685 | **0 — absent, never refreshed** |
| trench dominance (0.16) | `*_line_yards`, `*_stuff_rate` | 138/685 | **0 — absent, never refreshed** |
| true points per possession (0.16) | `pts_per_poss` | 303/685 | 301 — **present but frozen all season** |

Consequences, in order of importance:

1. **Finishing drives and trench dominance are a constant neutral 50 for every team, in every
   backtest run to date.** Their keys are absent from the baseline file, and
   `reconstruct_pit_team_data` never computes them. Together they are **0.32 of the 0.37
   efficiency weight ≈ 11.8% of the entire composite** — weight that currently carries zero
   team-to-team information in the backtest.
2. **Week 1 is the weakest point.** With no prior weeks the reconstruction explicitly forces
   `off/def_success_rate = None` and `epa_play = def_epa_play = 0.0` (neutral 50), so at week
   1 only the frozen `pts_per_poss` (16% of the bucket) carries any efficiency signal at all.
3. **True points per possession is stale, not neutral** — present from the baseline, then
   never refreshed, so it reflects a prior value all season.
4. **This is the most likely explanation for the backtest/live divergence.** The backtest
   reports 48.3% ATS while the live FBS-vs-FBS record is 41.6%; they are not measuring the
   same model, because live has all five sub-metrics populated and the backtest has two.
5. **Weight experiments are unsafe until this is fixed.** Tuning the efficiency weight (37%,
   the largest lever in the composite) against a backtest with half that bucket held at a
   constant would tune a number that does not describe production. Any arm testing
   efficiency-adjacent changes is measuring an artifact.

**Recommended fixes, for Jeff's decision — these touch model inputs, so they are not mine to
apply:**

- Populate `off_ppo`/`def_ppo`, `off_line_yards`/`def_line_yards`, `off_stuff_rate`/
  `def_stuff_rate` in the baseline from CFBD `/stats/season/advanced` (verified available:
  `pointsPerOpportunity`, `lineYards`, `stuffRate` for every season 2019–2025), and refresh
  them in-season like the other four;
- let week 1 fall back to prior values instead of forcing neutral;
- give `pts_per_poss` an in-season derivation so it stops being a baseline-only key.

Until then, **the backtest is a valid A/B comparator between weightings but not a faithful
model of the live pick pipeline** — and the divergence in §4.4 should be quoted whenever its
absolute numbers are used.

---

## 5. Leak rules the builder must enforce

- Never read season *Y* ratings/stats to project season *Y* games, at any week.
- Week *W* of season *Y* may use: season *Y−1* full-season data, season *Y* offseason facts
  (talent, recruiting, returning production), and season *Y* weeks `< W` results only.
- 2020 results are flagged unrepresentative, not silently mixed in.
- Every generated baseline records its provenance (which season each field came from) so a
  later reader can audit leak-freedom without re-deriving it.

---

## 6. What remains to build

1. `scripts/build_season_baselines.py` — assemble `data/backtest_cache/preseason_{Y}.json`
   per §1/§3, with provenance and the §5 leak assertions. *Not yet written.*
2. Season parameterisation of `scripts/run_model_backtest.py` (preseason file, lines, and
   the PIT week range all per season; the week range already derives from completed games).
3. Rig support for arms × seasons, archiving one row per (run, arm, season). Note the
   `backtest_runs` primary key is `(run_id, arm)`; a season suffix on `run_id` keeps it
   unique **without a destructive table rebuild**.
4. A 2-season proof run (2024 + 2025) before extending to the full range.

Cost is negligible: ~20 CFBD calls per season (~100 for five seasons) against a 20k/month
budget.