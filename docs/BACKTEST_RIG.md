# The Backtest Rig

Standing infrastructure for testing the composite model. It answers **"did this weighting
help?"** — and it answers it the same way next month, because experiments are archived
rather than remembered.

Owner: CTO. Model changes remain Jeff's call (CFO doctrine) — this rig *measures*; it never
decides and never touches production weights.

---

## Quick start

```bash
cd C:/Users/jtracy/dev/cfb-power-rankings
set -a; . ./.env; set +a          # CFBD_API_KEY (+ CF_D1_TOKEN for archival)

python scripts/backtest_rig.py --list                # what arms exist
python scripts/backtest_rig.py                       # run every arm, report + archive
python scripts/backtest_rig.py --arms baseline,zero-srs
python scripts/backtest_rig.py --refresh             # re-fetch inputs first (new games played)
python scripts/backtest_rig.py --history             # every archived experiment, newest first
python scripts/backtest_rig.py --no-d1               # local archive only
```

A full two-arm experiment runs in **under a second** on cached inputs, against ~4 CFBD calls
the first time.

---

## The three layers

### 1. Frozen inputs — `data/backtest_cache/`

`scripts/run_model_backtest.py` fetches four things (`stats/game/advanced` for weeks 1–3 and
`lines?year=2026`) and caches them to disk. `--refresh` re-fetches deliberately.

**This is an integrity mechanism first and a speed mechanism second.** Every arm of an
experiment must be evaluated against *the same bytes*. If the harness re-fetched between
arms, a CFBD update landing mid-experiment would silently become the difference between the
arms — and the experiment would look like it measured the weights when it actually measured
the data.

The cache also refuses to report a run over zero games: an empty payload would otherwise
produce a confident-looking summary with no games in it.

### 2. Arms — `data/backtest_arms.json`

An arm is a **named weight set**, i.e. data, not a shell invocation. That is what makes an
experiment repeatable and reviewable by someone else.

```json
"zero-srs": {
  "weights": { "sp_plus": 0.20454545, "fpi": 0.17045455, "srs": 0.0,
               "elo": 0.09090909, "talent": 0.11363636, "efficiency": 0.42045455 },
  "note": "SRS removed, its 12% re-normalised proportionally."
}
```

Rules the rig enforces before it runs anything:

- **Weights must total 1.0** across the enabled keys, or the rig exits. A miscalibrated arm
  produces confident garbage, which is worse than no result.
- **`weights: null` means the live defaults** — the `baseline` arm. Always include it: a
  treatment without a control on the same data is not an experiment.
- **Adding an arm is a proposal, not a change.** Nothing in that file affects production.

### 3. Archive — `backtest_runs` (D1) + local files

Every arm of every run is recorded, keyed by `(run_id, arm)`:

- **D1 `backtest_runs`** — the durable record, queryable months later. This is the layer the
  Cloudflare migration bought: history that outlives the machine that produced it.
- `data/backtest_runs.jsonl` — the local append-only mirror (also the offline fallback for
  `--history`).
- `data/backtest_runs/<run_id>/<arm>.{summary,fixture}.json` — the full per-game audit for
  each arm, so a surprising number can be taken apart game by game.

`model_version` (the hash of the exact weight set) is the audit key: an arm can never be
silently compared against a different one, because the hash would differ.

Archival is **best-effort and never fails an experiment** — if the token is absent or D1 is
unreachable, the run still reports and still archives locally.

---

## Columns recorded per arm

`run_id, ts_utc, arm, model_version, weights_json, season, games, fbs_matchups, ats_all,
ats_3star, ats_5star, totals_all, totals_3star, dog_share_pct, mean_model_margin,
mean_book_line, mae_vs_book, bias_vs_book, note`

The two margin diagnostics matter as much as the win rates:

- **`mean_model_margin` vs `mean_book_line`** — directly tests whether the model's margins
  are tighter than the market's (the compression hypothesis).
- **`mae_vs_book` / `bias_vs_book`** — how far the projections sit from the closing line,
  and in which direction.

---

## Querying history

```bash
python scripts/backtest_rig.py --history        # compact table, D1 first
```

Or directly:

```sql
-- every arm ever run, and whether each beat the baseline
SELECT run_id, arm, model_version, fbs_matchups, ats_all, totals_all, dog_share_pct
FROM backtest_runs ORDER BY ts_utc DESC;

-- one model_version over time (a weighting's record as games accumulate)
SELECT ts_utc, fbs_matchups, ats_all, totals_all
FROM backtest_runs WHERE model_version = 'ca2f071afda' ORDER BY ts_utc;
```

---

## What the rig does NOT do

- **It does not change the model.** Weights are applied through `COMPOSITE_WEIGHTS_JSON` for
  the duration of a run only. A winning arm must still be applied to `COMPOSITE_CONFIG` with
  Jeff's sign-off, which changes `model_version` and **deliberately** breaks comparability
  with historic rows.
- **It does not overwrite the reviewed baseline artifacts.** `data/backtest_summary.json`
  and `data/backtest_fixture_2026.json` are untouched by design (`--out-summary` /
  `--out-fixture`), verified byte-identical after runs.
- **It is not a live-performance record.** Arms evaluate a *point-in-time reconstruction*
  (week 1 from the frozen preseason baseline; later weeks only from completed prior games;
  closing lines frozen at kickoff). The live path additionally applies injury adjustments,
  wind, the underdog floor and live line overlays, and runs on mid-season composites. So
  **compare arms to each other, not to the live record** — the backtest's FBS ATS rate
  (48.3%) is better than the live FBS-vs-FBS record (41.6%) for these reasons.
- **It does not cover non-FBS matchups** — ATS/totals tiers are FBS-vs-FBS only, because
  that is where the demonstrable edge lives (see `docs/MODEL_BRIEFING_CEO_CFO.md` Part 10).

---

## First results in the archive

Run `20260923T005349Z`, 409 games / 155 FBS matchups, identical cached inputs:

| arm | model_version | ATS | O/U | dogs |
|---|---|---|---|---|
| baseline | `ca2f071afda` | 48.3% | 61.4% | 38% |
| zero-srs | `c0229dfa0f4` | 45.9% | 61.0% | 49% |

**Zeroing the stale SRS input made spreads slightly worse and left totals unchanged — the
stale input is not the cause of the spread losses.** Full write-up in
`docs/MODEL_BRIEFING_CEO_CFO.md` Part 10.

---

## Verification performed at bring-up

- Two-arm run completed; **the second arm made no network calls** (cache hit, no fetch lines).
- Report reproduced the independently-run numbers exactly (48.3% / 45.9% / 61.4% / 61.0%).
- D1 rows **read back** with correct values and distinct `model_version`s per arm.
- `--history` verified on the D1 path and the offline (no-token) fallback path.
- Published baseline artifacts `md5`-unchanged after every run.
- Weights validation exercised: an arm that does not total 1.0 is refused before running.