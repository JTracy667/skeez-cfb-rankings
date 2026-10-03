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
production composite** to reconcile against. Live right now: build **v65**, code marker
**v65-week-cutover-epa**, `model_version` **cc4e71b6d16**. The composite config is hashed into
`model_version`, so it is a change detector — if a weight moves, that string moves.

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
