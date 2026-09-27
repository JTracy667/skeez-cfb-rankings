# Advanced Stats & Matchup Intelligence Implementation Plan

**Target Assignee:** CTO (`@CTO_Repartedbot`)  
**Author:** Chief of Research & Intelligence  
**Date:** 2026-09-27  
**Status:** Approved for Implementation  
**Project:** `C:\Users\jtracy\dev\cfb-power-rankings`  
**Reference Document:** `docs/CFBD_API_MAP.md`, `docs/CFBD_COMPOSITE_INPUTS.md`

---

## 1. Executive Summary & Context

Jeff Tracy provided two high-level college football advanced analytics reference cards:
1. **Oregon @ USC Advanced Stats Preview:** Focuses on game projections, EPA margins, rush/pass success rate splits, Net Points per Drive, Starting Field Position, and Eckel Rate / Eckel Ratio / Points per Eckel (Quality & Finishing Drives).
2. **Collin Wilson / SporSource Matchup Card:** Focuses on Trench play (Line Yards, Stuff Rate, Havoc Rate, Pass Rush / Pass Blocking proxies), Down-and-Distance splits (Standard vs. Passing Downs Success Rate & Explosiveness), Strength of Record / Schedule (SOR / SOS), and Special Teams ratings.

### Crucial Architectural Finding: ZERO Additional Quota Cost
Our application **already calls** the exact CFBD endpoints that return these metrics in our nightly sync (`/stats/season/advanced`, `/ratings/fpi`, `/ratings/sp`, and `/ppa/teams`). Currently, our parser in `app.py` simply ignores the majority of keys in these payloads. 

By updating the extraction logic, we gain access to over 20 elite sharp metrics **without spending a single extra API call or exhausting CFBD quota**.

---

## 2. Phase 1: Data Ingestion & Storage Expansion

### 2.1 File Targets
* Primary: `app.py`
  * `_cfbd_advanced_stats()` (~line 1629)
  * `_cfbd_fpi()` (~line 1550)
  * `_cfbd_ratings_sp()` (~line 1520)
  * `fetch_live_analytics()` / `_build_analytics_dict()`
* Shared/Storage Artifacts:
  * `data/cfbd_analytics.json` (Local runtime cache)
  * `cfbd_shared.py` (Verify zero drift)

### 2.2 Fields to Ingest from Existing Endpoints

#### A. `/stats/season/advanced` (Already called in `_cfbd_advanced_stats()`)
Extract the following additional fields for both `offense` and `defense`:
* **Havoc Rates:**
  * `off_havoc_total`: `off.get("havoc", {}).get("total")`
  * `off_havoc_front_seven`: `off.get("havoc", {}).get("frontSeven")`
  * `off_havoc_db`: `off.get("havoc", {}).get("db")`
  * *(Repeat identical keys for `defense` as `def_havoc_*`)*
* **Eckel / Quality Drives Metrics:**
  * `off_drives`: `off.get("drives")`
  * `off_total_opportunities`: `off.get("totalOpportunies")`
  * `off_eckel_rate`: `round(off.get("totalOpportunies", 0) / off.get("drives"), 4)` (Guard against `drives == 0`)
  * `def_drives`: `defn.get("drives")`
  * `def_total_opportunities`: `defn.get("totalOpportunies")`
  * `def_eckel_rate`: `round(defn.get("totalOpportunies", 0) / defn.get("drives"), 4)`
  * `eckel_ratio`: `round(off_eckel_rate / (off_eckel_rate + def_eckel_rate), 4)`
* **Play Type Success Rate & Explosiveness Splits:**
  * `off_rush_success`: `off.get("rushingPlays", {}).get("successRate")`
  * `off_rush_explosiveness`: `off.get("rushingPlays", {}).get("explosiveness")`
  * `off_rush_rate`: `off.get("rushingPlays", {}).get("rate")`
  * `off_pass_success`: `off.get("passingPlays", {}).get("successRate")`
  * `off_pass_explosiveness`: `off.get("passingPlays", {}).get("explosiveness")`
  * `off_pass_rate`: `off.get("passingPlays", {}).get("rate")`
  * *(Repeat for `defense` as `def_rush_success`, `def_pass_success`, etc.)*
* **Down-and-Distance Splits:**
  * `off_standard_down_success`: `off.get("standardDowns", {}).get("successRate")`
  * `off_standard_down_explosiveness`: `off.get("standardDowns", {}).get("explosiveness")`
  * `off_passing_down_success`: `off.get("passingDowns", {}).get("successRate")`
  * `off_passing_down_explosiveness`: `off.get("passingDowns", {}).get("explosiveness")`
  * *(Repeat for `defense` as `def_standard_down_success`, etc.)*
* **Field Position:**
  * `off_field_pos_avg`: `off.get("fieldPosition", {}).get("averageStart")`
  * `def_field_pos_avg`: `defn.get("fieldPosition", {}).get("averageStart")`
  * `net_field_pos`: `off_field_pos_avg - def_field_pos_avg`

#### B. `/ratings/fpi` (Already called in `_cfbd_fpi()`)
Extract resume ranks:
* `fpi_sor`: `rec.get("resumeRanks", {}).get("strengthOfRecord")`
* `fpi_sos`: `rec.get("resumeRanks", {}).get("strengthOfSchedule")`
* `fpi_game_control`: `rec.get("resumeRanks", {}).get("gameControl")`
* `fpi_eff_special_teams`: `rec.get("efficiencies", {}).get("specialTeams")`

#### C. `/ratings/sp` (Already called in `_cfbd_ratings_sp()`)
Extract special teams:
* `sp_special_teams`: `rec.get("specialTeams", {}).get("rating")`

### 2.3 Phase 1 CTO Checklist
- [ ] Inspect `_cfbd_advanced_stats()` in `app.py` and verify all dictionary keys handle `None` gracefully without throwing `TypeError`.
- [ ] Add division-by-zero guards for `off_eckel_rate` and `def_eckel_rate` when `drives == 0`.
- [ ] Update `_cfbd_fpi()` to store `fpi_sor`, `fpi_sos`, and `fpi_eff_special_teams`.
- [ ] Update `_cfbd_ratings_sp()` to store `sp_special_teams`.
- [ ] Verify `data/cfbd_analytics.json` is updated and maintains valid JSON schema.
- [ ] Run `python -m pytest tests/` to confirm zero regression on existing composite and data pipelines.

---

## 3. Phase 2: Matchup Detail View (UI & API Delivery)

### 3.1 Objective
Provide a sharp matchup preview for any game on `/schedule` or `/analytics`, mirroring the Oregon @ USC layout:
1. **Head-to-Head Trench Matchup:**
   * Offense Rushing Success & Line Yards vs. Defense Stuff Rate & Rush EPA.
   * Offense Passing Success & Dropback EPA vs. Defense Havoc Rate & Passing Down Success.
2. **Quality Drives & Finishing:**
   * Eckel Rate (Quality Drives %) & Points Per Opportunity (Finishing Drives).
3. **Situational & Field Position:**
   * Starting Field Position, Standard vs. Passing Down Success, and Special Teams efficiency.

### 3.2 Backend Endpoint Implementation
* Create endpoint: `GET /api/matchup?home=<HomeTeam>&away=<AwayTeam>`
* Computes and returns:
  * Raw values for both teams.
  * **FBS Percentile / National Rank (1–138)** for each metric, enabling the frontend to color-code rows (e.g. Top 10 = Dark Green/Blue, Bottom 30 = Red).
  * Direct differentials (e.g. `Away Off Pass Success vs. Home Def Pass Success`).

### 3.3 Frontend Implementation
* Add an "Advanced Matchup Preview" toggle/drawer inside the existing schedule matchup tile or modal.
* **Layout Specifications (Strict Mobile-First Law):**
  * Base width tested down to **375px/390px** without horizontal scroll.
  * 3-column head-to-head comparison table:
    * Column 1: Away Team Value + National Rank badge.
    * Column 2: Centered Metric Label (e.g., `EPA/RUSH`, `ECKEL RATE`, `HAVOC`).
    * Column 3: Home Team Value + National Rank badge.
  * Clean visual cards matching site aesthetic (`bg-card`, dark mode styling, legible typography).

### 3.4 Phase 2 CTO Checklist
- [ ] Implement `GET /api/matchup` in `app.py` taking `home` and `away` parameters.
- [ ] Compute national rankings (1 to 138) across FBS for all ingested advanced stats to feed the rank badges.
- [ ] Build the UI modal/drawer in `schedule.html` (or separate shared template).
- [ ] Verify responsive layout at 375px, 390px, and 768px viewports (ensure logos and labels do not break flex containers).
- [ ] Ensure Dockerfile explicitly includes any new template or asset files so Render deploys cleanly.

---

## 4. Phase 3: Analytical Backtesting & Composite Modeling

### 4.1 Governance & Safety Mandate
* **RULE 1 (Strict Doctrine):** No changes to production weights in `COMPOSITE_CONFIG` (`app.py`) may occur without Jeff's explicit sign-off.
* **RULE 2 (No Unverified Changes):** All model experimentation must run offline against local historical fixtures on disk (`data/backtest_summary.json` / `_book_lines.json`).

### 4.2 Research Hypothesis
Current composite weight allocation:
```python
composite = (
    sp_norm * 0.18
    + fpi_norm * 0.15
    + srs_norm * 0.12  # (Note: SRS is 2025 fallback)
    + elo_norm * 0.08
    + talent_norm * 0.10
    + eff_norm * 0.37  # Efficiency lever
)
```
* **Hypothesis A (Eckel & Finishing Drives):** Teams with high Eckel differential (generating scoring opportunities while preventing them) outperform standard efficiency on spread margins.
* **Hypothesis B (Havoc & Passing Downs):** Teams with lopsided Havoc advantages cover at a higher rate as underdogs.
* **Hypothesis C (Replacing SRS):** Since `ratings/srs` is currently 0 rows for 2026 and relying on previous season fallback, reallocating part of the 0.12 weight into live `eckel_margin` and `havoc_differential` could eliminate stale-season drag.

### 4.3 Backtest Experiment Setup
* Script to create/extend: `scripts/backtest_advanced_arms.py`
* Test Arms:
  * **Arm 0 (Control):** Current production `ca2f071afda` weights.
  * **Arm 1 (Eckel Enhanced):** Blend 5% Eckel Margin into the efficiency cluster.
  * **Arm 2 (Havoc / Trench Adjusted):** Add Havoc differential to defensive efficiency.
  * **Arm 3 (Live Season Hybrid):** Replace SRS fallback with Eckel Ratio + Havoc Rate.
* Evaluation Criteria:
  * Strict walk-forward ATS (Against The Spread) win rate.
  * 3-Star Totals ($≥ 4.0$ pts edge) ROI.
  * Mean Absolute Error (MAE) vs. closing margin.

### 4.4 Phase 3 CTO Checklist
- [ ] Construct `scripts/backtest_advanced_arms.py` reading strictly from local fixtures.
- [ ] Run walk-forward backtest over completed 2025/2026 games.
- [ ] Generate comparative delta table (`docs/BACKTEST_ADVANCED_METRICS.md`) reporting ATS %, Totals %, and CLV impact.
- [ ] Submit report to Research & Jeff before touching any live composite formulas.

---

## 5. Verification & Acceptance Sign-off

Before declaring this project complete:
1. `pytest tests/` passes 100%.
2. Staging and Prod parity probe returns HTTP 200 for `/api/matchup` and `/api/analytics`.
3. CFBD quota consumption remains flat (verified via response headers `X-CallLimit-Remaining`).
4. UI passes mobile responsiveness checks on iOS/Android viewports.
