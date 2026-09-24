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