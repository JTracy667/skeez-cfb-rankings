# QA ADDENDUM — v34 fixes + triage of the v33 QA verdict

**Written by:** CTO, 2026-09-23
**Context:** QA ran `docs/QA_BRIEF_SITE_V33.md` against live v33 and returned a REFUTED verdict
with 2×P1 + 3×P2 + 1 source-level risk. Each item was re-verified against live prod and the
source before anything was changed. Result: **1 P1 real, 1 P1 refuted, 3 P2 confirmed**,
3 model-rule items held for Jeff (not touched — they are betting-model behaviour).

---

## 1. Triage — every QA item, with the evidence

| QA item | Verdict | Evidence |
|---|---|---|
| P1 · Best Bets empty (handler pinned to Week 1) | **CONFIRMED — fixed** | `api_best_bets()` called `api_schedule_fetch(week=1)`. Live `GET /api/best-bets` → `{"best_bets":[],"count":0}`. Week-1 games are all in the books, so the board was empty for the rest of the season. |
| P1 · 13 FCS opponents all composite 15 (regression trigger) | **REFUTED — my brief's fault** | Massey FCS ranks are live: map = **128 teams**, differentiated. South Dakota State (rank 14) → **33.0 (ranked)**, Howard (98) → 15.0, Incarnate Word (28) / Montana (29) → 15.0 (just outside the top-25 cutoff). 15.0 IS the designed "unranked FCS" tier; the tier is coarse, so several unranked opponents legitimately share it. The trigger I wrote in the brief ("all identical = bug") was wrong and is corrected in the brief. |
| P2 · Cross-page projections disagree (Rutgers–Howard 41.1–7.4 vs 27–20.1) | **CONFIRMED — root cause found, HELD for Jeff** | Schedule applies a **two-pass opponent-suppression** projection; Win Totals projects each side off its own composite vs the opponent's, with **no suppression pass**, so Rutgers reads 27.0 pts against every opponent. Also visible in win prob: schedule 47.5% vs win-totals 0.909 for the same game. Fixing it changes win-total numbers → **calibration/model territory, needs Jeff's work order.** |
| P2 · `experience_contribution` missing on all 25 rows | **CONFIRMED — fixed** | Computed in `project_score_multi_factor`, never copied onto the row by `_enrich_with_composite`. Experience is an enabled input at 0.1856 of the composite, so the published breakdown could not be reconciled. |
| P2 · `input_vintages` returned but not rendered | **CONFIRMED — fixed** | 0 references in all four pages. The rankings board now prints input provenance in its footer, including that SRS is the PRIOR season. |
| P2 · `GET /api/schedule` returns week 0 / no matchups | **CONFIRMED — fixed** | It read the persisted `week_schedule.json` and served `{"week":0,"matchups":[]}` while the page (POST fetch) showed 71. Now defaults to the live week via the page's own path. |
| Risk · projection floors/clamps (0/6/3) in source | **CONFIRMED as source-level — HELD for Jeff** | `max(6.0, base_score)`, `max(3.0, …)` in `project_head_to_head`, `max(0.0, …)`, a `±4.0` net-PPG tilt clamp and a blowout `floor_share` shave. No live score was proven distorted. **These are model-shape rules, not display bugs — not mine to change.** |

### Not raised by QA, found during verification
- **Win probability was not a matchup quantity.** `home_win_prob` / `away_win_prob` carried each
  side's OWN composite logit (Rutgers 48.7 vs Howard 15.0 → 47.5% / 5.7% while the market had it
  −43.5; Maryland 75.8 / UCLA 78.3 — both above 50 in one game). Now the game margin logistic.
  This value is written to D1 `model_predictions`, so bad values were landing in the record.
- **Massey name-matching gap.** North Dakota State resolves to `rank=None` → lands on the 19.0
  fallback instead of the ~33.0 a top-5 FCS program earns (SDSU resolves fine). Alias/mapping gap.
  **Reported, not fixed** — source interpretation is Research territory.

## 2. Fixes shipped in v34 (no weights, no decay, no slope/HFA touched)

1. `/api/best-bets` — resolves the **live** week (`current_season_week`) instead of week 1.
2. `/api/schedule` — defaults to the live week through the same path the page uses.
3. Win probability — game margin logistic (complementary by construction), same function Win
   Totals uses.
4. `experience_contribution` — now on every ranking row.
5. Rankings board footer — renders `input_vintages`, marking prior-season vintages (SRS).
6. `POST /api/rankings/refresh` — `source` relabelled `cfb+poll` (was `espn`, which read as if the
   board came from the ESPN stub; it is a CFBD-seed rebuild and only the poll leg is ESPN).

### Local boot receipts (patched tree, before deploy)

```
GET /api/best-bets    count=56, 10 picks returned   (was count=0, 0 picks)
GET /api/schedule     week=4, 71 matchups           (was week=0, 0 matchups)
Rutgers–Howard        win probs 100.0 / 0.0         (was 47.5 / 5.7)
GET /api/rankings     rows=25, rows with experience_contribution=25   (was 0)
Georgia contributions sp 15.8 / rec 27.7 / eff 30.8 / exp 8.7
```

## 3. Still open, needs a decision (not changed)

- **Win Totals projection consistency** — apply the same suppression pass as the Schedule page?
  This CHANGES published win-total numbers and interacts with the `_H2H_SCALE=3.0` fit against 138
  book lines → needs Jeff's work order.
- **Projection floors/clamps** — 6.0 / 3.0 floors, ±4.0 tilt clamp, blowout `floor_share`. They
  contradict the "projections are passed through raw" doctrine. Change or keep? Jeff's call.
- **Massey alias gap** — NDSU-class teams on the fallback. Research owns source interpretation.

## 4. For QA's next pass

- Re-read §5 of the brief: the FCS trigger is corrected, and `experience_contribution`,
  `input_vintages`, `/api/schedule` and best-bets are now asserted differently.
- v34 changes three API responses — expect `count>0` on best-bets, a real week on
  `/api/schedule`, and complementary win probs (sum ≈ 100, never both above 50).
- Everything else in the brief is unchanged and still yours.

---

# PENDING BATCH (v35) — queued, NOT deployed

Jeff's standing note, 2026-09-23: no further deploys until the fix set is complete.
These are in the working tree and verified locally, awaiting one batched bump.

## 5. Win Totals now uses the Schedule page's model (Jeff's directive)

Root cause of the cross-page disagreement QA filed (P2): `compute_win_totals()` projected
each side with a **single-pass** `project_score_multi_factor` — no head-to-head pass, no
opponent suppression — so a team's projected score was its own average repeated against
every opponent (Rutgers read **27.0 in all 12 games**). The Schedule page uses
`project_head_to_head` (total from combined strength, margin from the composite gap).

Now:
- every Win Totals game is projected with **`project_head_to_head`**, the same function the
  Schedule page calls;
- each game is projected **in its own week** (that is what drives the talent decay), which is
  what the Schedule page does for the week it shows;
- the injury and wind terms are shared code — `matchup_conditions()` — called by BOTH pages,
  so the two can no longer drift apart by reimplementing the same overlay;
- injuries/weather are applied only to the live week's games. They are "active now" facts plus
  published weather; carrying today's injury report into November would be wrong, and the
  Schedule page has no weather for future weeks either.

**Acceptance:** for every game on the live slate, the Schedule page's `home_proj`/`away_proj`
must equal Win Totals' `proj_for`/`proj_against` for the same teams. QA should assert exactly
that, team by team, rather than eyeballing one game.

## 6. Deploy verification interval corrected (tooling, no image change)

`sleepAfter` was cut to 5m and `cfb_deploy.sh`'s own comment says 360s is the steady-state
probe interval, but the code default stayed at **1260s (21m)** — a "for one run" value that was
never lowered. Consequence: verification time was "the first probe after the old instance
recycles", i.e. **39 seconds on v33** (container already idle) but **21m42s on v34** (container
warm at deploy time). Default is now **360s**, matching the 5m sleepAfter: worst case ≈ 12m,
typical ≈ 6m. The 6m figure was quoted before it was enforced — it is enforced as of this commit.

---

# FLAGGED FOR FOLLOW-UP TESTING (Jeff's call, 2026-09-23)

Verdict on the projection floors/caps: **KEEP AS-IS, but test them.** The clamps were
inside `project_head_to_head()` when the V6.1 constants were fitted, so removing them
now would invalidate that fit. They are not a display artefact bolted on afterwards —
`run_model_backtest.py`, `backtest_study.py`, `backtest_v2.py`, `backtest_v4.py`,
`preflight_v61.py` and `selftest_fcs_prior.py` all `import app` and call
`app.project_head_to_head(...)`, i.e. every fitted run went through them.

Provenance: `8627cb2` (2026-08-30) introduced the underdog cap and the 3.0 final floor
as part of the "Aug 30 recalibration (user call)"; `297eacd` (2026-08-30) the
`max(6.0, base_score)` floor; `56ed2f9` (2026-08-14) the ±4.0 net-PPG tilt.

## Inventory and how often each one actually binds (live 2026 slate, 888 FBS-vs-FBS games)

| Site (app.py) | What it does | Binds on |
|---|---|---|
| 3305, 3309 | `total = max(24.0, …)` after injury / wind | **0 of 888** — dead in practice |
| 3316-3329 | underdog cap: composite gap > 30 caps the weak side's share of the total (0.50 → 0.10 at gap 66) and hands the difference to the favorite; **rewrites both scores after the margin was computed** | **42 of 888** |
| 3330-3331 | `max(3.0, home_score)` / `max(3.0, away_score)` | 11 FBS sides + every FCS blowout (Georgia–Tennessee State **58.9-3.0**) |
| 3134, 3140 | `max(6.0, base_score)`; `±4.0` net-PPG tilt — these live on `project_score_multi_factor`'s **`score`**, and the head-to-head path consumes **`composite`**, so they do NOT reach the Schedule/Win Totals numbers | not on this path |

## The test to run (backtest-only; no live change without Jeff's sign-off)

1. ATS + MAE with the underdog cap ON vs OFF, out-of-sample, on the same 2021-24 fit /
   2025 holdout split used for V6.1 — the risk is that the cap was absorbing a fit
   error that a better margin curve would fix.
2. The 42 gap > 30 games specifically: does capping the underdog's share beat the
   closing line, or is it systematically shaving the wrong side?
3. The 3.0 floor: how many live sides were truly projected below 3.0, and does pinning
   them cost ATS? (An FCS side pinned at exactly 3.0 is the visible tell.)
4. Confirm `max(6.0)` / `±4.0` are genuinely inert on the head-to-head path, then either
   remove them or note them as no-ops.

Owner: CTO (backtest), CFO (any calibration sign-off), QA (assert the live numbers after
any change). Not to be bundled with a routine deploy.