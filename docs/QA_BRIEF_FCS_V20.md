# QA BRIEF — FCS prior rewrite (build v20)

**Requested by:** CTO · **For:** QA · **Status:** ready to send, pending Jeff's go
**Prod:** https://skeezcfb-rankings.com — **expected build stamp: `v20`**

---

## 1. What changed and why

The FCS (non-FBS opponent) projection had two hand-tuned stop-gaps left over from
before any FCS data existed:

- `fcs_composite = 16.0` — a **flat constant**, so every FCS opponent was projected
  identically. North Dakota State and a bottom-tier FCS school got the same number.
- `FCS_BLOWOUT_BOOST = 15.0` — an extra +15 pts added to FBS-vs-FCS margins.

Both are replaced with a **fitted, rating-derived** prior, using Massey's FCS
ratings (a new source — CFBD's `/ratings/massey` returns 0 rows).

The margin formula is `margin = (comp_home − comp_away) + 2.5·HFA + boost`, so the
composite gap maps 1:1 onto points. That makes the FCS composite directly
identifiable from real results:

```
comp_fcs = comp_fbs + HFA − actual_margin
```

Fitted over 103 completed 2026 FBS-vs-FCS games:

| FCS opponent | old | new |
|---|---|---|
| unranked | 16 (+15 boost) | **15** |
| ranked, Massey top 25 | 16 (+15 boost) | **33** |
| no rating available | 16 (+15 boost) | **19** |

Measured mean absolute error on those 103 games: **21.5 pts → 13.6 pts**.

**Also in this same image** (please treat as part of the review, not background):

- `model_version` on `rankings_daily` / `model_predictions` changed from the literal
  string `"composite"` to a **config hash** (`c<hash>`). Risk-register requirement:
  a weight change must produce a *visible* version break in the archive.
- `model_predictions` now actually writes (it had 0 rows, had never fired).

---

## 2. What to verify

Report **PASS / FAIL / INCONCLUSIVE** per item with the raw output. Do not take my
local test results as evidence — the point is independent reproduction.

### 2.1 Live build is v20
```bash
curl -s -A 'Mozilla/5.0' https://skeezcfb-rankings.com/api/health
```
Expect `"build":"v20"`. A warm container can serve the previous build for up to
~20m after a deploy, so if this reads `v19`, wait and re-check before proceeding —
testing v19 would invalidate items 2.4 and 2.5.

### 2.2 Differentiated FCS projections (the whole point of the change)
Pull the current week's slate and find games with an FCS side:
```bash
curl -s -A 'Mozilla/5.0' -X POST \
  'https://skeezcfb-rankings.com/api/schedule/fetch?week=4&year=2026'
```
A **top-25 Massey FCS opponent** and an **unranked** one must project with
**different** margins. Expect a spread of about **18 pts**. If every FCS opponent
still projects identically, the change did not take effect.

⚠ **Note:** the week-4 slate I checked has 13 FBS-vs-FCS games and **all of them are
unranked opponents**, so every one legitimately reads composite 15.0 — that is
correct, not a failure of differentiation. If the week you pull is the same, verify
differentiation with the offline check instead, which exercises real ratings:

```bash
REFRESH_INTERVAL_SECONDS=0 python scripts/selftest_fcs_prior.py
```
Expect **13/13 PASS**, including Montana State (#1) → 16.3 projected margin vs
Nicholls (#47) → 34.3, a gap of 18.0.

### 2.3 Independently reproduce the fitted constants
Do not read my numbers — regenerate them:
```bash
python scripts/export_d1.py          # D1 -> local SQLite
python scripts/fit_fcs_composite.py  # prints the implied composites
```
If your derived values differ materially from **15 / 33 / 19**, that is a finding —
say so with your numbers.

### 2.4 No residual boost — statistical, NOT a closed-form identity
The retired boost added a flat **+15** to every FBS-vs-FCS margin. So check the
residual rather than an exact identity:

```
residual = differential − ((home_composite − away_composite) + HFA)
```

Across all FBS-vs-FCS games on the slate, the **mean residual must be near zero**.
If the boost were still applied it would read **≈ +15**. Measured reference:
**−1.8** across the 13 week-4 FBS-vs-FCS games.

⚠ Individual residuals of a few points are **EXPECTED and are NOT bugs.** The margin
also carries an injury term, a wind term, and an underdog floor that rewrites the
scores *after* margin is computed. All three are documented in the
`project_head_to_head` docstring. (I initially wrote this item as an exact identity
and it produced 6 false failures out of 13 — that was my error, not the model's.)

Also assert the FCS side's `composite` is one of **15 / 33 / 19** — never the old
flat 16.

### 2.5 No regression on FBS-vs-FBS games
Same reasoning — judge residuals statistically, not as an exact identity, because
the injury / wind / underdog-floor terms apply to every game. Spot-check several
FBS-vs-FBS games, including one neutral-site game, and confirm residuals are small
and **centred on zero** rather than systematically offset. Additionally:
```bash
python scripts/selftest_composite_config.py
```
This asserts the composite weights still equal the documented originals
(0.18/0.15/0.12/0.08/0.10/0.37) and sum to 1.0. I claim the weights refactor was
**behaviour-preserving** — please try to falsify that rather than confirm it.

### 2.6 Pages still render (not just HTTP 200)
```bash
node scripts/page_render_check.cjs
```
Expect row counts: `/` 25, `/analytics` ~686, `/schedule` ~700, `/win-totals` ~276,
and **zero site-side script errors**. A 200 with an empty table is a FAIL.

### 2.7 Archive versioning actually changed
Query D1 and confirm recent `rankings_daily` / `model_predictions` rows carry
`model_version` starting with `c` (a hash), **not** the string `composite`.

---

## 3. Known limits — do NOT report these as bugs

These are documented in the code with the same wording; re-reporting them as
defects is noise:

1. **~13.6 pts of error remains.** One composite per group is a coarse model. Known
   and accepted for now.
2. **The rank curve is NOT monotone in the data.** Rank 1–5 implies composite 34.8
   while rank 6–15 implies 36.7 (n=5 and n=10). Most likely because top FCS teams
   schedule *stronger* FBS opponents, not because they are weaker. This is why only
   a 2-level split (ranked / unranked) is implemented.
3. **The change is explicitly flagged for further review and fine-tuning** by Jeff.
   Massey's full 1–128 ordering is loaded and intended as the input for that.
4. Individual game misses are expected (e.g. NC State 73–0 Richmond — the new value
   undershoots). Judge the *distribution*, not single games.

---

## 4. Scope boundaries

- **Do not change model constants.** If a number looks wrong, report it — do not
  "fix" it. Betting-model calibration changes require Jeff's sign-off.
- **Do not modify anything in the repo** unless asked. This is a read/verify pass.
- Ops POST routes need header `X-Admin-Token` (value in `./.admin_token`,
  gitignored). `analytics/fetch` and `schedule/fetch` are deliberately **not**
  token-gated — public pages cannot hold a secret.

## 5. Artifacts

- Repo: `C:\Users\jtracy\dev\cfb-power-rankings`
- D1: `c3ec3149-cc85-483b-b727-5a18e3d5a1b9` (token `CF_D1_TOKEN` in repo `.env`)
- Key scripts: `scripts/fit_fcs_composite.py`, `scripts/selftest_fcs_prior.py`,
  `scripts/selftest_composite_config.py`, `scripts/page_render_check.cjs`,
  `scripts/export_d1.py`, `massey_fcs.py` (the scraper)
- Commits: `67a7fc0` (scraper + evidence), `a7075a1` (composite config + D3 hash),
  `0ad9e08` (FCS prior + boost removal)

## 6. Reporting back

Per item: PASS/FAIL/INCONCLUSIVE + the raw command output. For any FAIL, include
what you expected vs what you saw. **Items 2.3 and 2.5 are the ones I most want
independent eyes on** — 2.3 is independent reproduction of a fitted constant, 2.5
is falsifying my "no behaviour change" claim. A clean bill on everything else plus a
real finding on either of those is a more useful report than all-PASS.
