# SESSION HANDOFF — HISTORY & LESSONS (CFB / skeezcfb-rankings.com)

> **STATE DOES NOT LIVE HERE.** Live state, procedures and open work are in
> **`OPERATIONS.md`** (repo root) and **`docs/DATA_FLOW.md`**. This file is kept only for
> its *history and hard-won lessons* (the line-source design, the backtest rig narrative,
> the credential-probe traps) — never for current state.
>
> Its original state section claimed a live build of **v27** while production was already
> far ahead; that section has been **removed** so it cannot mislead a session again.

Repo: `C:\Users\jtracy\dev\cfb-power-rankings`

---

## 1. HISTORY — the "Decision A" line source (shipped v27)

**Problem:** the displayed line came from PropLine `/best-line` (cross-book consensus over 18 books), so the
edge shown was measured against a price Jeff often **cannot bet**.

**Fix:** a line from one of **Jeff's own books** is the source of record and OUTRANKS the consensus.
Consensus now only fills a market his book has not posted (per-market, so a missing total still falls back).

- `app.py` → `_BOOK_OF_RECORD = ("betonlineag", "betmgm", "williamhill_us")`
- merge precedence inverted for the book of record; new `kind` value **`book_of_record`**
- **both** consensus guards now skip `book_of_record` and **LOG** a >6pt divergence instead of silently
  swapping in the consensus (that override would have quietly undone this change)
- `_BOOK_PRIORITY` (bulk fallback) = `betonlineag, betmgm, williamhill_us, draftkings, fanduel, betrivers, bovada`
  — **pinnacle removed by request** (also EU-region only in The Odds API)

**Live verification:** `/api/odds` → 714 entries; `book_title` counts **BetOnline.ag 80**, FanDuel 58,
DraftKings 1, plus Polymarket 108 / Fliff 5 / Kalshi 1 from the consensus path.

**Tests:** `scripts/selftest_book_of_record.py` — 15 assertions, including two no-regression cases
(consensus still wins when his book is absent; a non-Jeff book can never become the source).

---

## 2. JEFF'S BOOKS — mapping is settled, do not re-litigate

| Book | Feed key | Notes |
|---|---|---|
| BetOnline.ag | `betonlineag` | **primary**; sharp offshore lines he trusts |
| BetMGM | `betmgm` | |
| William Hill Nevada | `williamhill_us` | feed TITLES this key **"Caesars"** — the app is WH-branded but prices off Caesars lines. **Correct mapping.** Do NOT "fix" it to say William Hill. |
| Circa Sports | — | **NOT carried** by The Odds API (no `circa` key in us/eu/uk). Jeff: not a blocker. |
| Pinnacle | — | removed by request |

---

## 3. BACKTESTING RIG — where the work stands

**Phase 1 (historical line harvest): COMPLETE.**
- `scripts/harvest_odds_history.py` — 77 week-seasons (2021–2025), ~40 MB in `data/odds_history/` (not committed)
- The Odds API historical endpoint: archive floor **2020-10-03**; cost is **10 credits PER MARKET**, so
  `spreads,totals` = **20/snapshot** (an earlier note said 10/call — wrong)
- Total spend ~4,440 credits against a 7,500 cap
- **Coverage** (`scripts/odds_history_coverage.py` owns this definition — do not recompute by hand):
  4,970 games observed · **4,199** with open/close derivable · **2,133** with betonlineag at *both* ends
  · 1,720 betmgm · 2,670 williamhill_us
- **Metric trap:** do NOT measure coverage by "appears in the OPEN file and the CLOSE file" — snapshots are
  weekly, a game simply isn't posted yet at the open timestamp. Pool every snapshot per game and take
  earliest/latest **before its own kickoff**. The wrong method reports 285 and understates by 10×.

**Phase 2 (V4 arms): NOT STARTED — this is the next work item.**
- ARM 1 — combined: power 1.7 + real line movement (`close−open`) × move_factor, slope refit
- ARM 2 — market-anchor frame on **BetOnline.ag's opening line** (NOT Pinnacle), composite_edge × k
- ARM 3 — totals model with scales FITTED on 2021-24 by MAE (never guess the scales)
- **Standing fitting rule: fit on MAE, never ATS alone.** Maximizing ATS alone rewards shrinking the model
  (slope collapsed to 0.014, SU 61.1%, MAE 11.66 — degenerate). This retroactively explains the V2
  fine-tune's edge-pinned params.
- Report format: ATS + O/U + SU, per-season band, held-out 2025 separate, mean|margin| vs book, MAE/bias,
  fitted params, and **flag any grid-edge optima**.

**Phase 3 (PropLine daily snapshotting): NOT STARTED.** Forward-test only — PropLine has NO history
(`/odds/history` 404, no open/close fields). Plan: one file write per day to `data/odds_snapshots/YYYY-MM-DD.json`
alongside the existing pull. It is a **live-path code change** → deploy with a smoke check.

**Reference docs:** `docs/BACKTEST_STUDY_5Y.md`, `docs/BACKTEST_V2.md`, `docs/BACKTEST_V2_FINETUNE.md`,
`docs/BACKTEST_V3.md`, `docs/ODDS_API_MAP.md`, `docs/PROPLINE_API_MAP.md`, `docs/CFBD_API_MAP.md`.

**V3 result (for continuity):** nothing clears the 52.4% break-even. Best ATS = nonlinear power 1.7 at
**51.3%** (held-out 51.2%, tightest band 2.3pp); best calibration = line-move arm MAE **6.71**.
ARM 3 (the intended ship candidate) was WORSE than arm 1 alone — 50.6% vs 51.3%.

---

## 4. DEPLOY CREDENTIALS — the traps (this cost hours; read it)

`scripts/cfb_deploy.sh vNN <code-marker>` — takes the tag, bumps `wrangler.jsonc`, builds, preflights,
deploys, then **verifies** by polling `/api/health` until `code.marker` equals the new marker
(auto-rollback on 500/timeout). The verify window is long: the container serves the OLD image until it
recycles (`sleepAfter` **5m** idle), so budget **~6–12 min** typical. Probe interval must exceed
`sleepAfter` or the instance never sleeps and the new image can never come live → false rollback.

**The script REQUIRES `CLOUDFLARE_API_TOKEN` in the environment.** Resolve it with the canonical helper —
**never by grepping caches**:

```bash
export CLOUDFLARE_API_TOKEN="$(cat "$(python "$LOCALAPPDATA/hermes/profiles/cto/scripts/cf_deploy_token.py")")"
export CLOUDFLARE_ACCOUNT_ID=90c2c31beec12cb7de1c249ade1eb773
bash scripts/cfb_deploy.sh vNN <code-marker>
```

`cf_deploy_token.py` prints the **path** of the token file (never the value — that
contract changed 2026-10-03 after a printed token forced a roll), valid for the first
credential that actually passes `/accounts/<id>/containers/me`, and caches it to
`profiles/cto/.cf_deploy_token` (outside `cache/`, so it
is not pruned). The old procedure — grepping `profiles/cto/cache/terminal/*.sh` — is **superseded and
dangerous**: that directory is pruned at 24h idle, which is exactly how the v45 deploy died with
"CLOUDFLARE_API_TOKEN is not set".

### TWO TRAPS THAT WASTED HOURS — do not repeat

1. **The value is QUOTED in the cache.** A pattern without the quote
   (`CLOUDFLARE_API_TOKEN=[A-Za-z0-9_-]{20,}`) matches **nothing** and silently returns empty — which looks
   exactly like "the credential was deleted". It was never deleted; the grep was wrong.
2. **`/user/tokens/verify` is NOT a valid probe for this token.** It returns
   `"Invalid API Token"` for the account-scoped `cfat_` token **even though the token works fine**.
   Prove validity against the real endpoint instead:
   `GET /accounts/90c2c31beec12cb7de1c249ade1eb773/workers/scripts` → 200 = good.
   (Verified capabilities: Workers/Containers deploy, Zone DNS Edit, Zone Settings write.
   NOT: Zone WAF / bot management.)

**Lesson:** when something "disappears", verify against the **real** endpoint before reporting a missing
or revoked credential. A bad probe plus a bad grep produced a confident false alarm to Jeff.

---

## 6. STANDING RULES THAT APPLY HERE

- **Read `OPERATIONS.md` + `docs/DATA_FLOW.md` before any action on this repo.**
- **Any betting-model calibration change or site-visible number change needs Jeff's sign-off first.**
- Never change what external data sources track or how parsers interpret them (Research territory).
- Never trust `node --check`: verify page JS by executing it in a Node vm sandbox against the live API.
- Any new page must be added to the **Dockerfile `COPY` line** — a page missing from it 500s in prod.
  Verify the COPY line lists every `*.html`.
- Rankings page sorts by **composite rank ALWAYS**; AP/Coaches columns are reference-only, never sortable.
- Schedule data comes from CFBD `/games`; ESPN scoreboard truncates to 25 events/week — never wire it back.
- Jeff wants **headline + receipts** for status asks; forensics only on request.

---

## 6. WHAT TO DO FIRST IN A NEW SESSION

Read `OPERATIONS.md` → its **RECENT CHANGES & OPEN WORK** section lists the current priority order.
Do not resume from this file.