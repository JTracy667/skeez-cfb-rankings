# V6.1 deploy + data collection (2026-09-23)

Work order: "V6.1 DEPLOY + DATA COLLECTION WORK ORDER". Four parts, one reference model.

## Part 1 — V6.1 live

| | before (v28) | after (v29) |
|---|---|---|
| SP+ | 0.18 | 0.185567 |
| efficiency | 0.37 | 0.309278 |
| talent | 0.10 | 0.319588 |
| experience | — | 0.185567 |
| FPI / SRS / Elo | 0.15 / 0.12 / 0.08 | 0.00 (dropped) |
| HFA | 2.5 | 3.5 |
| margin | `gap * 1.0 + HFA` | `sign(gap) * |gap|^1.1 * 0.65 + HFA` |

`experience` was absent from the live composite entirely — the data existed
(`_cfbd_roster_experience`, 0–100) and was in no weight. Injury normalizer, wind penalty and
the underdog floor are untouched.

The order's 3-dp weights (0.186/0.309/0.320/0.186) sum to **1.0010**, which trips
`_assert_weights_sum`. Deployed values are the exact renormalized shares: 0.185567 /
0.309278 / 0.319588 / 0.185567 = **1.000000**. Same weights, exact arithmetic.

### Preflight (weeks 2–4 2026, 866 games, `scripts/preflight_v61.py`)

| metric | old | new |
|---|---|---|
| mean \|margin\| | 7.17 | 6.46 |
| median \|margin\| | 2.50 | 3.50 |
| p90 \|margin\| | 22.60 | 14.43 |
| max \|margin\| | 71.2 | 50.1 |
| games \|margin\|>55 | 8 | 0 |
| games \|margin\|>40 | 20 | 3 |
| mean \|margin\| where old gap>30 (44 g) | 43.0 | 26.0 |

Sample games (same fixtures, both runs):

- Kent State @ Ohio State 61.1–3.0 (margin +58.1) → 50.6–3.0 (**+47.6**); market ≈ −45
- Presbyterian @ Western Carolina 27.7–22.2 (+5.5) → 28.3–21.4 (+6.9)
- WKU @ Georgia 61.4 → 38.7

Verdict: the new weighting compresses the absurd tail while typical games move by the +1.0
HFA bump. Nothing looked off, so the deploy proceeded (the order's own condition).

**Honest limit:** no V5–V8 arm compared V6.1 against the FORMER live formula — those arms
were compared among themselves against the market. So this is consistency with the fitted
model, not a demonstrated ATS improvement.

## Part 2 — injury_snapshots

New D1 table (18 cols), unique on `(game_id, team, model_version)`. One row per team per
game, written pre-kickoff by `d1_write_path.snapshot_injuries()`, called from the same
hourly pass as `model_predictions`. Clean games write an empty list — that is the control
group. `actual_*` / `residual_*` stay NULL and are filled by a later reconciliation pass.

## Part 3 — PropLine daily archive

`_maybe_snapshot_propline()` keeps the full multi-book response once per day: the file the
order asked for (`data/odds_snapshots/YYYY-MM-DD.json`) **and** a `raw_payloads` row. The
row is what accumulates — Cloudflare Containers have an ephemeral filesystem
(`d1/schema.sql:158`), so a file-only snapshot survives until the next recycle and no
further. PropLine publishes no history endpoint.

## Part 4 — weather

`model_predictions` gains `wind_mph`, `temp_f`, `condition`, `indoor`, `wind_penalty`. CFBD
`/games/weather` is fetched per request with no historical feed; previously the only copy
died with the response.

## Migration discipline

`CREATE TABLE IF NOT EXISTS` cannot alter the live table, so the weather columns exist in
two places: `d1/schema.sql` (fresh DBs) and `d1/migrations/2026-09-22_data_hooks.sql` (the
live `cfb-history` DB). Applying BOTH to the same fresh DB errors with `duplicate column
name` — expected; the migration is for databases that predate the columns. Applied to live
D1 and verified: 5 weather columns, `injury_snapshots` 18 cols + 3 indexes.

## Verification

Offline harness (`scratch/test_data_hooks.py`, D1 stubbed): schema/upgrade equivalence, 2
rows per game with correct per-team sign, empty list preserved, **both writers refuse a game
whose kickoff has passed**, weather round-trip, gzip+b64 payload round-trip.