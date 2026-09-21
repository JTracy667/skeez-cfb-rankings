# API CAPABILITY CATALOG

Live-verified inventory of every external API the app touches. Built 2026-09-21
(CTO). Companion machine-readable index: `docs/api_catalog.json`.

Rule: **nothing here is assumed.** Every row marked VERIFIED-LIVE was probed with
our own keys on 2026-09-21; JUNK/EMPTY verdicts come from the live response, not docs.

Hosts found in the repo code paths:
`api.collegefootballdata.com`, `api.prop-line.com`, `api.the-odds-api.com`,
`site.api.espn.com`, and scraped surfaces `www.covers.com`, `www.on3.com`,
`www.puntandrally.com`.

---

## 1. CFBD — api.collegefootballdata.com
Auth: `Authorization: Bearer <CFBD_API_KEY>`. Plan: paid (Tier 2). *Plan numbers
conflict across sources — one directive says 5,000/day, another 30K/month; needs
one authoritative confirmation (CFO).*

| endpoint | used by app | live probe (2026-09-21) | division | verdict |
|---|---|---|---|---|
| `/teams?year=` | yes | 682 rows; `id, school, mascot, abbreviation, alternateNames, conference` | all | OK |
| `/ratings/elo?year=` | yes | 138 rows; depth ≥2000 (2000→123 rows) | FBS | OK |
| `/ratings/fpi?year=` | yes | 138 rows; `fpi, resumeRanks, efficiencies` | FBS | OK |
| `/ratings/sp?year=` | yes | 139 rows; `rating, ranking, secondOrderWins` | FBS | OK |
| `/ratings/sp?…&division=fcs` | — | **139 rows — identical to FBS** | — | **JUNK** |
| `/ratings/srs?year=` | yes | **0 rows for 2026** | FBS | **EMPTY** |
| `/talent?year=` | yes | 138 rows | FBS | OK |
| `/recruiting/teams?year=` | yes | 221 rows; `rank, points` | all | OK |
| `/ppa/teams?year=` | yes | 136 rows (2025); `offense, defense` | FBS | OK |
| `/stats/season?year=` | yes | 8,568 rows (2025); long-format `statName/statValue` | FBS | OK |
| `/records?year=` | yes | 682 rows; `classification, division` | all | OK |
| `/roster?year=` | yes | 31,283 rows; athlete-level | all | OK |
| `/games?year=&week=` | yes | 231 rows (2024 wk1); `id, season, week, seasonType, startDate` | all | OK |
| `/drives?year=&week=` | yes | 4,242 rows (2024 wk1) | all | OK |
| `/lines?year=&week=&seasonType=regular` | yes | 125 rows (2024 wk1); historical closing lines | all | OK |
| `/player/returning?year=` | yes | not yet probed live | — | UNPROBED |
| `/stats/season/advanced?year=` | yes | not yet probed live | — | UNPROBED |

**CFBD coverage note:** our app calls ~16 of CFBD's much larger endpoint surface;
this catalog currently covers the ones we use + the SP+ trap. Full-surface sweep
(~100 endpoints) is a follow-on pass — see GAPS.

## 2. The Odds API — api.the-odds-api.com
Auth: `apiKey=<THE_ODDS_API_KEY>`.
- Plan: **~20,000 credits** — live header 2026-09-21: `x-requests-remaining: 19600`,
  `x-requests-used: 400`. (A directive claimed "free tier 500/month" — WRONG.)
- `/v4/sports` — VERIFIED-LIVE, 200, listed sports. Used by app: **fallback only**
  (PropLine is the primary odds feed).

## 3. PropLine — api.prop-line.com
Auth: `PROPLINE_API_KEY` (header/param TBD by probe).
- Plan: 5,000 requests/day. Live app log: 4,038/5,000 used (81%), 962 left; app
  auto-degrades to bulk-only below 20%.
- Surface: endpoint paths NOT yet enumerated — **VERIFIED-DOCS/UNPROBED**. This is
  the real tightest budget in the stack. Used by app: **yes — primary odds/lines feed.**

## 4. ESPN — site.api.espn.com
- `/apis/site/v2/sports/football/college-football/rankings` — used by app for AP +
  Coaches poll columns. Free, no key. VERIFIED-LIVE (via app behavior).

## 5. Scraped surfaces (no API key)
`www.covers.com`, `www.on3.com`, `www.puntandrally.com` — referenced in code paths
(HTML scraping). Fragile by nature; treat as UNVERIFIED/unstable. Not probed.

---

## KNOWN JUNK / GAPS (do not re-probe blind)
1. **CFBD `/ratings/sp` + `division=fcs` → JUNK.** Live 2026-09-21: returns the SAME
   139 FBS rows as the FBS query. The filter is silently ignored; FCS SP+ is NOT
   available this way. Any "division" style param must be live-tested the same way.
2. **CFBD `/ratings/srs?year=2026` → EMPTY (0 rows).** SRS is not populated for the
   current season. The app reads `srs` — worth a staleness guard.
3. **The Odds API plan is ~20K, NOT free 500/mo** (see §2). Do not budget against 500.
4. **Plan figures disagree across directives** for CFBD (5,000/day vs 30K/month).
   Needs one authoritative source.
5. Full CFBD endpoint surface is not yet swept; this catalog covers used + trapped
   endpoints only.

## VERIFICATION RECEIPT
All probes: 2026-09-21, `api.collegefootballdata.com` with our key, read-only GETs
(~20 calls — far inside budget). Raw results mirrored in `docs/api_catalog.json`.