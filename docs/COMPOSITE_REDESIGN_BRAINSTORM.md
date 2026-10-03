# Composite Redesign Brainstorm — From-Scratch Design Notes

**Status:** BRAINSTORM ONLY — nothing in this doc is implemented or approved. No
weight, formula, or code change has been made as a result of this document.
**Author:** Research (chief of research & intelligence)
**Date:** 2026-10-02
**Trigger:** Pittsburgh landed at composite rank #2 (84.8) despite being unranked
in AP/Coaches polls, SP+ rank 20, FPI rank 22, recruiting rank 44 — while
holding the weakest strength-of-schedule rank in the model's fpi_sos field
(119 of ~134 FBS teams). Investigation traced this to the *local repo's*
V6.1-labeled composite formula, which is almost certainly stale against the
live v6.2+ production system (the one with 42 advanced stats landed in D1
after the local repo's V6.1 comments were written).

## Important caveat on sourcing

Research does **not** currently have a live connector to query production D1
directly (the CTO note at the end removes this limit without any token).
Everything cited about "current weights" in the earlier part of this
investigation came from reading the local git repo (`app.py`,
`docs/CFBD_COMPOSITE_INPUTS.md`), which Jeff flagged as outdated relative to
what's actually deployed. Treat any specific current-weight numbers from that
earlier pass as **unverified / likely stale** until Research has either:
- read-only production D1 access (scoped token or a debug/export endpoint), or
- a field list / sample export of the 42 advanced stats now in D1.

> **UPDATE 2026-10-03 (CTO) — both caveats are resolved, see the CTO note at the end.**
> The repo was stale because **GitHub was 43 commits behind production**; it is now pushed and
> current, and the deployed formula is the one in `app.py`. The field list needs no D1 token —
> `/api/analytics` is public. The text below is preserved as the original working record.

Everything below is a **from-scratch design proposal**, intentionally
independent of whatever the current live formula actually is.

---

## Design philosophy for a CFB power-ranking composite

### 1. Opponent-adjustment is the foundation, not a bucket
Raw box-score/efficiency differentials compared against a fixed league-average
constant is how a team beating up a bottom-10-nationally schedule (fpi_sos 119)
can max out every efficiency sub-metric simultaneously and rocket to #2. The
correct approach — what SP+, FPI, and Massey all do under the hood — is an
**iterative / ridge-regression opponent adjustment**: solve for each team's
offensive and defensive rating simultaneously so "Team A's efficiency vs Team
B" nets out B's own quality automatically. This replaces "normalize against a
fixed 50" everywhere in the model, not just as a patch on top.

### 2. Three honest layers, not one flat weighted sum of unlike things
- **Results layer** (what actually happened): opponent-adjusted margin of
  victory (SRS-style) + Elo. Cheap, robust, hard to game.
- **Process layer** (why it happened): opponent-adjusted down-to-down
  efficiency — success rate split by standard/passing downs, EPA/PPA,
  finishing drives (PPO), trench stats. Should predict *future* results better
  than results alone, but only once genuinely opponent-adjusted (see #1).
- **Prior layer**: talent/recruiting/returning production — real signal early,
  should decay as a function of *games played + schedule quality actually
  seen*, not just calendar week. A team with a brutal week 1-4 schedule should
  decay its prior differently than one that padded a record against four
  FCS-level opponents — decay should be information-weighted, not a fixed
  week-number curve.

### 3. Fit the blend empirically, not by committee
Don't hand-assign shares like "sp_plus = 18%, talent = 32%." Run a regression
(ridge / elastic-net, or OLS with cross-validation) of final-score margin or
ATS outcome against the layer scores, walk-forward across seasons, and let the
data set the weights. Re-fit each offseason. This is the only way to know
whether an efficiency bucket really deserves 30-44% of the composite, or
whether that share is an artifact of it being the one bucket with no
opponent-adjustment compensating in the fit.

### 4. Garbage-time and sample-size hygiene
- Exclude garbage-time snaps from efficiency stats (CFBD's `/ppa` and
  `/stats/season/advanced` endpoints generally support this filter) — a 45-0
  blowout's junk-time plays shouldn't count the same as competitive snaps.
- Don't trust single-metric ceilings for teams under ~4-5 games; blend harder
  toward the prior when the in-season sample is thin, regardless of week
  number.

### 5. No hard 0-100 clamps on raw differentials
Clamping is how a dominant-but-unadjusted stat silently loses all information
once it "maxes out" — exactly what happened to Pittsburgh (4 of 5 efficiency
sub-metrics pinned at the ceiling simultaneously, flattening "crushed a bad
schedule" and "crushed a good schedule" into the identical number). Use a
continuous transform (z-score against the league distribution, or a logistic
squash) so extremes still separate from each other instead of collapsing.

### 6. Turnover/luck regression
Fumble recovery and INT rate are close to a coin flip at the team level, so
raw turnover margin shouldn't move a power rating much. Regress hard toward
zero before use. (Consistent with the existing standing rule: 50% regression
toward zero.)

### 7. Validation discipline
True walk-forward backtest — point-in-time, zero future leakage, **weekly**
granularity (not season aggregates, which is what the one backtest run so far
in this repo actually used, and which that report itself flags as not
leakage-free). Score on BOTH:
- straight-up accuracy / rank-correlation (what a power ranking is for), AND
- ATS calibration, scored separately — conflating the two is how a model ends
  up with good rankings but bad bets, or vice versa.

---

## Confirmed stat inventory (from the Jeff-personal matchup card, 2026-10-03)

Jeff's `scripts/matchup_report.py` matchup card (admin-gated `/api/matchup`,
stored analytics, no live API cost) confirms the following per-team,
per-opponent-matchup metrics are already stored and surfaced — this is the
real candidate input list for any redesign, independent of what the local
repo's composite formula currently reads:

**Trench & Havoc** — off success rate, off explosiveness, off line yards, off
stuff rate, off rush success, off pass success, def havoc rate (+ split by
front-7 / DB), def stuff rate, def line yards allowed, def rush SR allowed,
def pass SR allowed.

**Quality Drives & Finishing** — Eckel rate, Eckel ratio, off/def points per
opportunity (PPO), off/def points per drive.

**Situational Downs** — off/def standard-down success rate, off/def
passing-down success rate.

**Field Position & Special Teams** — net field position, SP+ special teams,
FPI special teams, **Strength of Record**, **Strength of Schedule**.

**Confirms the Pittsburgh diagnosis directly:** Strength of Record and
Strength of Schedule are both already stored per team (seen live: USC SOS
rank 61 vs Washington SOS rank 131 in the sample card) — this is exactly the
opponent-adjustment signal the composite-formula investigation found missing
from the live weighting. It's sitting in stored analytics already; the open
question is whether/how it's folded into the composite, not whether it
exists.

Also notable and not seen referenced in the local repo's composite formula:
havoc rate (overall + front-7/DB split), Eckel rate/ratio (drive-quality,
distinct from PPO), and separate SP+ vs FPI special-teams ratings.

## Open question for next step

What are the 42 advanced stats now in production D1? Some CFBD advanced-stat
endpoints already carry partial opponent-adjustment baked in; others are still
raw box-score numbers that would need the regression treatment in #1. Can't
scope real work (which of these need adjustment, which are redundant /
collinear with existing inputs, which are strong enough to include at all)
without either production D1 read access or a field list / sample export.

## Status / next actions

- No code, weight, or formula change has been made.
- Awaiting: (a) a way to read live production D1 (scoped read token, debug
  endpoint, or export) so future analysis is against ground truth, not the
  local repo snapshot; (b) Jeff's direction on which of the above threads
  (opponent-adjustment engine, empirical weight refit, soft-clamp, decay
  redesign, turnover regression, walk-forward validation harness) to scope
  into an actual work order.
- Per standing CFO doctrine, any actual composite weight change requires
  Jeff's sign-off before shipping.

---

## CTO note (2026-10-03) — how to read the real thing

**GitHub is now current — re-read it.** `main` in `JTracy667/skeez-cfb-rankings` was **43 commits
behind production** when the staleness above was noticed. That was the whole cause: the repo was
genuinely out of date, so reasoning from it produced an out-of-date picture. It was pushed
2026-10-03 and now matches what is live. The same is true of `hermes-org`. Don't trust a cached
clone — re-pull.

**The repo IS the live formula.** Production is deployed from the working tree
(`scripts/cfb_deploy.sh <tag>`), not from GitHub. So `COMPOSITE_CONFIG_DEFAULT` in `app.py` *is*
the current weighting, and the V6.1 comments around it are accurate — there is **no separate newer
production composite** to reconcile against. Live right now: build **v66**, code marker
**v66-adv-stats-from-d1**, `model_version` **cc4e71b6d16**. The composite config is hashed into
`model_version`, so it is a change detector — if a weight moves, that string moves.

**We are in week 5.** Week 6 is in the future and has not been played. The archive contains rows
stamped `week=6` because a pull is stamped with the week it TARGETS, and Friday's pull targets the
upcoming slate — week 6 opens with Tuesday games, so the pull lands about four days ahead of them.
**That is deliberate, not a mislabel**, and not a writer bug. Do not read `week=6` rows as a
completed week.

**You do NOT need production D1 access to answer the open question.** Everything stored per team is
on a public endpoint, no token:

```bash
# The browser UA is REQUIRED: Cloudflare answers 1010 to the default curl/urllib UA.
curl -s https://skeezcfb-rankings.com/api/analytics -H 'User-Agent: Mozilla/5.0' \
  | python -c "import sys,json; d=json.load(sys.stdin); t=d['teams']; print(len(t),'teams'); print(sorted(t[0].keys()))"
```

That prints the complete field list of a real team record — the definitive answer to "what are the
42 advanced stats", including which of them are composite inputs. Related endpoints:

| endpoint | gives you |
|---|---|
| `/api/health` | live build + code marker + `model_version`, quota budget, `archive.count` |
| `/api/analytics` | every stored per-team field; its `serve` field says which door answered |
| `/api/schedule?week=N` | model projection, win %, line, weather, records per game |

**Read the `serve` object before citing any number from `/api/analytics`.** `{source: d1,
degraded: false}` is the real thing; a `disk (…)` source with `degraded: true` means D1 was
unreachable and you are reading a fallback copy. Citing a degraded payload is how a stale number
gets into an analysis.

Source-of-truth pointers inside the repo: `ADV_MATCHUP_FIELDS` and `ADV_MATCHUP_PPA_FIELDS` in
`app.py` enumerate the advanced stats the matchup engine ranks; `docs/DATA_FLOW.md` maps every
dataset to its producer → store → reader; `OPERATIONS.md` `CURRENT STATE` is the fleet-wide answer
to "what is live right now".

**Confirmed from source, independent of the endpoint:** the §5 soft-clamp concern is real, not
theoretical. All five efficiency sub-norms (`sr_norm`, `epa_norm`, `ppo_norm`, `trench_norm`,
`ppd_norm`, around `app.py:3609-3658`) end in `max(0, min(100, …))`, so simultaneous ceiling hits
genuinely collapse into one indistinguishable number — exactly the Pittsburgh pattern.

---

## CTO questions for Research (2026-10-03)

Everything below is a question, not a proposal — Research owns data-source interpretation
(`/stats/season/advanced` vs `/ppa/teams` semantics, what carries baked-in opponent
adjustment), so I am not going to guess at it. What I can do is tell you exactly what is
stored and hand you the access, so the analysis runs against ground truth.

### What is answerable right now

- **The 48-key candidate set is enumerable.** `MATCHUP_METRICS` in `matchup_engine.py` (or
  `ADV_MATCHUP_FIELDS` in `app.py`) is the exact list the matchup engine ranks; those names are
  the same `stat_key` values stored in D1, so the two can be joined without a translation table.
- **The publication marker is the live key list.** D1 `app_state['analytics_publication:2026']`
  carries `week`, `stamp`, `n_rows`, `n_teams`, `keys[]` (115 today), and a `digest`. That is
  the authoritative "what is actually served".
- **Weekly history is retained, append-only.** `stat_observations` keeps every pull
  (e.g. Alabama `off_success_rate` has 10 rows across Sep 28 → Oct 3), so a weekly walk-forward
  backtest has real point-in-time rows rather than one overwritten snapshot.

### A. Opponent adjustment (blocks #1 and #3 of the design philosophy)

1. **Which of the 48 candidate keys already carry opponent adjustment, and which are raw
   box-score numbers?** The doc asserts `/ppa` and `/stats/season/advanced` *generally* support
   filtering/adjustment. I need it per-field, not per-endpoint, because the model would otherwise
   double-count adjustment on some keys and miss it on others.
2. **Does any CFBD advanced endpoint expose an opponent-adjusted form directly**, or is every
   adjusted number something we have to derive ourselves? If derivable, from what minimum inputs
   (per-game results + per-team rates), and is that data in D1 or CFBD-only?
3. **Is `fpi_sos` and `fpi_sor` computed on the same scale across weeks**, or does it get
   re-based? A weekly backtest that compares week-4 SOS to week-9 SOS needs them comparable.

### B. Redundancy and collinearity (blocks "which are strong enough to include")

4. **Correlation matrix across the 48 keys, and against the existing composite inputs**
   (`sp_plus`, `fpi`, `elo`, `srs`, `composite`). Which are strongly collinear — havoc vs
   stuff rate, Eckel rate vs PPO, SP+ ST vs FPI ST — such that including both adds no
   information and just double-weights one concept?
5. **`net_field_pos` is stored but I cannot tell if it is an input or decoration.** Same question
   for `off_eckel_rate` / `eckel_ratio` / `def_eckel_rate`: in or out?

### C. The live composite's current shape

6. **Four of the named inputs are zero-weighted in the live config** — `fpi`, `srs`, `elo` are
   `_ENABLED_KEYS`-excluded (present and named, but inert, and JSON overrides for them are
   silently ignored). Is that deliberate, and does it change the Pittsburgh reading? A composite
   that ignores SRS while ranking a weak-schedule team #2 is a different story than one that
   weights it.
7. **What are the actual live bucket shares?** I can read them off `COMPOSITE_CONFIG_DEFAULT`;
   what I am asking for is whether the *observed* behaviour (Pittsburgh #2) is explained by those
   shares or by the clamps in §5 — those are two different fixes and I do not want to ship the
   wrong one.

### D. Sample size, decay and garbage time

8. **What is the right "as of" boundary for a weekly point-in-time join?** Rows are stamped with
   the week a pull TARGETS, which can be the upcoming week — Friday's pull targets week 6 because
   week 6 opens with Tuesday games. So on 2026-10-03 the archive legitimately holds both `week=5`
   and `week=6` rows while the site's `current_season_week()` correctly returns 5 (week 6 has not
   been played). For walk-forward validation I need to know whether the correct cutoff is the pull
   week, the last completed game week, or the publication stamp — and whether the stored values are
   cumulative-to-date or single-week (Alabama's `off_success_rate` is byte-identical across
   successive pulls, which reads cumulative-and-unchanged rather than per-week). This determines
   whether a `(week, stamp)`-selected snapshot can pick up rows for a week that has not been played.
9. **Garbage-time exclusion: available or not?** If not available from CFBD, is there a
   defensible proxy (score margin filter, play count), or should the design drop it?
10. **Prior-layer decay inputs** — `talent_score`, `recruiting_*`, `returning_ppa`,
    `roster_count` are all in the published 115 keys. What is the proposed information-weighted
    decay (games played × schedule quality seen), and does it need data we do not store?

### E. Validation harness

11. **What is the minimum walk-forward design you would accept as leakage-free at WEEKLY
    granularity?** Specifically: the point-in-time join, and whether the published-snapshot
    stamp (rather than `MAX(recorded_at)`) is the correct "as of" boundary — I believe it is,
    because that is what the site actually served at the time.
12. **`closing_lines` and `games` are both in D1** (365k odds rows, 21k games). Is the
    stored closing line sufficient to score ATS calibration separately from rank correlation,
    as §7 requires, for prior seasons — or only for 2026?

### F. Scope

13. **Of the six threads** (opponent-adjustment engine, empirical weight refit, soft-clamp,
    decay redesign, turnover regression, walk-forward harness) — which one has to go first for
    the others to be measurable? My read is the validation harness, because without it every
    other change is unfalsifiable, but that is your call to argue.

### Caveats I would rather state than have you discover

- **A whole section can vanish without any row being lost.** A published snapshot is selected by
  ONE `(week, stamp)`. The pull that became the publication carried 67 of 115 keys, so 36
  advanced metrics were absent from the key list and 22 of 34 matchup rows read blank while D1
  held the values at other stamps. When a whole bucket looks empty, check
  `analytics_publication:<season>.keys` before concluding the source is down.
- **The composite's weights need Jeff's sign-off to ship.** Backtest changes are authorised by a
  work order; pushing a changed number live is not.
- **I am not the right owner for source semantics.** If a question above amounts to "what does
  this field mean", it is yours, and I would rather it come back as a correction than as a
  workaround.

---

## How to read D1 for this analysis (Research) — 2026-10-03

A **read-only** D1 token now exists so this work runs against ground truth instead of the repo.
It cannot write: `CREATE TABLE` returns *"You do not have permission to perform this operation."*
Expires **2026-10-17**; ask the CTO to re-mint if you need it longer.

```
TOKEN FILE : C:\Users\jtracy\AppData\Local\hermes\profiles\cto\.cf_d1_readonly_token
DATABASE   : cfb-history   uuid c3ec3149-cc85-483b-b727-5a18e3d5a1b9
ACCOUNT    : 90c2c31beec12cb7de1c249ade1eb773
```

**Never commit the token, and never print its value** (the resolvers print a PATH for this reason).

### Setup

```bash
export CF_D1_TOKEN="$(cat "C:/Users/jtracy/AppData/Local/hermes/profiles/cto/.cf_d1_readonly_token")"
cd C:/Users/jtracy/dev/cfb-power-rankings
```

```python
import sys; sys.path.insert(0, ".")
import d1_store
rows = d1_store.query("SELECT COUNT(*) AS n FROM stat_observations")
```

`d1_store.query(sql, params)` takes `?` placeholders and returns `list[dict]`. Everything below
is copy-pasteable.

### The tables worth knowing

| table | rows (2026-10-03) | what it is |
|---|---|---|
| `stat_observations` | 431k | **the metric store.** `subject_type='team'`, `subject_id`=CFBD team id, `stat_key`, `value`, `season`, `week`, `recorded_at`, `source`. Append-only. |
| `app_state` | — | key/value. Holds **`analytics_publication:2026`** — the authoritative served snapshot. |
| `teams` | — | `team_id`, `name`, `abbr`, `conference`, `classification`, `first_season`. Join key for names. |
| `games` | 21k | `game_id`, `season`, `week`, `home_id`, `away_id`, `kickoff`, scores, `status`, `neutrality`. |
| `closing_lines` | 7,184 | stored closing line. Columns `game_id, book, spread_home, total, home_moneyline, away_moneyline, captured_at` — **note `home_moneyline`/`away_moneyline` here vs `home_ml`/`away_ml` in `odds_snapshots`.** |
| `odds_snapshots` | 365k | `game_id`, `book`, `spread_home`, `total`, `home_ml`, `away_ml`, `poll_ts`. Overwhelmingly **2026** (369k rows) — older seasons have only hundreds. Use `closing_lines`, not this, for multi-season work. |
| `model_predictions` | 330 | `game_id`, `win_prob_home`, `predicted_margin_home`, `predicted_total`, weather columns. |
| `weather_snapshots` | 112k | per-game weather by poll, incl. `kickoff_utc`. |
| `slate_cache` | 19 | `kind` in (schedule, rankings, win_totals), `week`, `payload_gz` — the exact payloads the site serves. |
| `api_usage` | — | the quota ledger (bucket/day/month, source, calls). |
| `backtest_runs` | — | existing backtest records. |
| `served_snapshots` | 385 | per-endpoint serve receipts (`endpoint`, `as_of`, `row_count`, `content_hash`, `build_tag`). |

### The three things that will bite you

1. **A snapshot is selected by ONE `(week, stamp)`.** The served board is not "the newest rows".
   A pull that wrote 67 of 115 keys became the publication once, and 36 metrics read blank while
   D1 held them at other stamps. Always read the marker before concluding a metric is missing:

```python
import json, d1_store
pub = json.loads(d1_store.query(
    "SELECT value FROM app_state WHERE key='analytics_publication:2026'")[0]["value"])
print(pub["week"], pub["stamp"], len(pub["keys"]))     # -> 5, ..., 115
```

2. **`week` is the week a pull TARGETS, not the last completed week.** Friday's pull targets the
   upcoming slate (week 6 opens Tuesday), so the archive legitimately holds `week=6` rows while the
   site correctly says week 5. Do not treat `week=6` rows as a played week (see question 8).

3. **`recorded_at` is the pull timestamp; the same metric is stored many times.** For a
   point-in-time value, filter on `recorded_at <= <cutoff>` — not just `season`/`week`.

### Worked queries for the questions above

**Q4 — correlation matrix.** Latest value per team per key, then pivot in pandas:

```python
KEYS = ["off_success_rate","off_explosiveness","off_line_yards","off_stuff_rate",
        "off_havoc_total","def_havoc_total","off_eckel_rate","eckel_ratio","off_ppo",
        "def_ppo","pts_per_poss","net_field_pos","sp_special_teams","fpi_sor","fpi_sos",
        "sp_plus","elo","srs","fpi","composite"]
marks = ",".join("?" for _ in KEYS)
rows = d1_store.query(
    "SELECT t.name AS team, s.stat_key AS k, s.value AS v, s.week AS w, s.recorded_at AS ts "
    "FROM stat_observations s JOIN teams t ON t.team_id = s.subject_id "
    f"WHERE s.subject_type='team' AND s.season=2026 AND s.stat_key IN ({marks})", KEYS)
# keep the LATEST row per (team, key), then pivot to team x key and .corr()
```

**Q1/Q2 — field inventory across the season:**

```python
d1_store.query("SELECT stat_key, COUNT(*) AS n, COUNT(DISTINCT subject_id) AS teams "
               "FROM stat_observations WHERE season=2026 GROUP BY stat_key ORDER BY stat_key")
```

**Q8 — is a stored value cumulative or per-week?** Look at one team across pulls:

```python
d1_store.query(
    "SELECT week, value, recorded_at FROM stat_observations "
    "WHERE subject_type='team' AND subject_id=(SELECT team_id FROM teams WHERE name='Alabama') "
    "AND stat_key='off_success_rate' AND season=2026 ORDER BY recorded_at")
```

**Q12 — ATS scoring inputs.** **Answered: prior seasons are covered.** Verified coverage:

| season | scored games | stored closing lines |
|---|---|---|
| 2026 | 1,361 | 521 |
| 2025 | 3,829 | 1,547 |
| 2024 | — | 1,507 |
| 2023 | — | 1,347 |
| 2022 | — | 1,413 |
| 2021 | — | 849 |

So ATS calibration can be scored on multiple seasons, not just 2026. Re-check it yourself:

```python
d1_store.query(
    "SELECT g.season, COUNT(*) AS n FROM closing_lines c "
    "JOIN games g ON g.game_id=c.game_id GROUP BY g.season ORDER BY g.season DESC")
```

**Opponent-adjustment work (Q1–Q3)** — per-game results join cleanly to team ids:

```python
d1_store.query(
    "SELECT g.season, g.week, g.game_id, th.name AS home, ta.name AS away, "
    "g.home_score, g.away_score, g.neutrality "
    "FROM games g JOIN teams th ON th.team_id=g.home_id JOIN teams ta ON ta.team_id=g.away_id "
    "WHERE g.season=2026 AND g.home_score IS NOT NULL ORDER BY g.week, g.game_id")
```

### One caution

The read-only token is scoped to **account-level D1 read** (Cloudflare offers no per-database
scoping for this permission group). It can read every D1 database in the account, not just
`cfb-history`. That is why it is read-only and time-boxed.
