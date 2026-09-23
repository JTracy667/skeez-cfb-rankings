# The Odds API Map — capability surface

**Maintainer:** CTO · **Created:** 2026-09-23
**Purpose:** what this API can and cannot give us, every claim live-verified. Sibling of
`docs/CFBD_API_MAP.md`. Field-level *semantics* stay Research's territory; this map covers
**transport and availability**.

Regenerate: `python scripts/probe_odds_apis.py` and `python scripts/probe_odds_apis_detail.py`
(write `data/odds_api_surface.json` + `data/odds_api_detail.json`). Read-only GETs.

Verdicts: **VERIFIED-LIVE** = 200 with a non-empty body · **EMPTY** = 200/404 with empty body ·
**JUNK** = error or unusable · **UNPROBED** = not attempted.

---

## 1. Basics

| | |
|---|---|
| Base URL | `https://api.the-odds-api.com/v4` |
| Auth | `apiKey` **query parameter** (no header form observed) |
| Quota signal | headers `x-requests-remaining`, `x-requests-used`, `x-requests-last` |
| Observed | **19,465 remaining of 20,000** (`used` 535) |
| Cost model | `x-requests-last` = markets × regions on an odds call (3 markets × 1 region = **3**); a historical call cost **10** |
| Budget for this sweep | **23 credits** (512 → 535) |

---

## 2. Endpoint table

| Endpoint | Params | Verdict | Measured | Cost |
|---|---|---|---|---|
| `/v4/sports` | `apiKey` | **VERIFIED-LIVE** | 80 active sports | 0 |
| `/v4/sports` | `all=true` | **VERIFIED-LIVE** | 179 (incl. outrights) | 0 |
| `/v4/sports/americanfootball_ncaaf/events` | `apiKey` | **VERIFIED-LIVE** | 71 events, 14.8 KB | 0 |
| `/v4/sports/{sport}/odds` | `regions,markets,oddsFormat` | **VERIFIED-LIVE** | 71 events, 326 KB | 3 |
| `/v4/sports/{sport}/odds` | `regions=us,eu,uk` | **VERIFIED-LIVE** | 71 events, 370 KB | 3 |
| `/v4/sports/{sport}/scores` | `daysFrom=3` | **VERIFIED-LIVE** | 71 events, 18 KB | 2 |
| **`/v4/historical/sports/{sport}/odds`** | `date=` | **VERIFIED-LIVE** | **52 events, 146 KB** | **10** |
| `/v4/sports/{sport}/events/{eventId}/odds` | `regions,markets` | **VERIFIED-LIVE** | 10 bookmakers, 3.4 KB | 1 |
| `/v4/sports/{sport}/odds`, `markets=player_pass_yds` | — | **JUNK 422** | `INVALID_MARKET` — props not on this endpoint | 0 |
| `/events/{badId}/odds` | — | **JUNK 422** | `INVALID_EVENT_ID` | 0 |

## 3. HISTORICAL ODDS — the headline finding

**Historical odds are available on this plan.** This is the finding that matters most to the
modelling programme: it makes the V3 line-movement arm **backtestable** instead of forward-test
only.

```
GET /v4/historical/sports/americanfootball_ncaaf/odds
    ?apiKey=***&date=2025-10-01T00:00:00Z&regions=us&markets=spreads&oddsFormat=american
```

Response shape: `{timestamp, previous_timestamp, next_timestamp, data[]}` — `data[]` holds the
same event/bookmaker/market/outcome structure as the live endpoint.

**Snapshot semantics (measured):** for `date=2025-10-01T00:00:00Z` the response returned
`timestamp=2025-09-30T23:55:39Z`, `previous=23:50:39Z`, `next=00:00:39Z` — i.e. **5-minute
snapshots**, and the timeline is **walkable** via `previous_timestamp` / `next_timestamp`. So we can
reconstruct open→close movement for any past date ourselves rather than relying on a stored
open/close pair.

- Cost **10 credits** per call. A full 5-season daily walk is a real spend — plan the cadence
  before harvesting (e.g. one snapshot per game per day, not per 5 minutes).
- Oldest available date is **UNPROBED** — verify the archive floor before promising 5 years.

## 4. Sports / leagues

Football keys available (verbatim):

- **`americanfootball_ncaaf`** — our primary
- **`americanfootball_ncaaf_fcs`** — **FCS is a separate sport key.** Directly relevant: we model
  FBS-vs-FCS games, and this is an independent line source for the FCS side.
- `americanfootball_ncaaf_championship_winner` (outright)
- `americanfootball_nfl`, `americanfootball_nfl_preseason`, `americanfootball_nfl_super_bowl_winner`
- `americanfootball_cfl`, `americanfootball_ufl`

## 5. Bookmakers (27 observed, `regions=us,eu,uk`)

`betanysports, betmgm, betonlineag, betrivers, betsson, betus, bovada, boylesports, coolbet,
coral, draftkings, everygame, fanatics, fanduel, gtbets, ladbrokes_uk, leovegas, leovegas_se,
lowvig, matchbook, mybookieag, nordicbet, onexbet, pinnacle, unibet_nl, unibet_se, williamhill_us`

**Sharp books present:** `pinnacle`, `lowvig`, `betonlineag`, plus `matchbook` (an exchange).
**Circa is NOT available.** No `bookmaker.eu`, no `betcris`.

## 6. Player props and alternate markets — per-event ONLY

Props are **rejected on `/odds`** (`422 INVALID_MARKET`) but **work on the per-event endpoint**.
Measured against a live event:

| Market | Verdict | Books returning it |
|---|---|---|
| `team_totals` | **VERIFIED-LIVE** | 2 |
| `alternate_spreads` | **VERIFIED-LIVE** | 6 |
| `player_pass_tds` | **VERIFIED-LIVE** | **1** |
| `player_pass_yds` | **VERIFIED-LIVE** | **1** |
| `player_rush_yds` | **VERIFIED-LIVE** | **1** |
| `player_receptions` | supported, **0 books** | 0 |

**Read the coverage column before building on props.** Player props exist but are carried by
**one book**. A single book is not a market — there is no consensus to compare against and no way
to identify a stale price. `alternate_spreads` (6 books) and `team_totals` (2) are the usable
per-event markets today.

## 7. Field-level schema

Event: `away_team, bookmakers, commence_time, home_team, id, sport_key, sport_title`

Bookmaker: `key, title, last_update, markets[]`

Market: `key, last_update, outcomes[]`

Outcome: `name, price, point` (point null for h2h/outrights)

Sport object: `key, group, title, description, active, has_outrights`

**Note:** unlike PropLine, this API exposes **no per-outcome change timestamps**. `last_update`
sits at bookmaker/market level, so "when did this price move" is coarser here. For movement, the
historical endpoint's 5-minute snapshots are the better instrument.

## 8. Explicit historical-availability answer

| Question | Answer |
|---|---|
| Historical odds obtainable? | **YES — VERIFIED-LIVE**, 5-minute snapshots, walkable |
| Sports covered historically | tested on `americanfootball_ncaaf`; others **UNPROBED** |
| Archive floor | **UNPROBED** — unknown how far back |
| Cost | 10 credits/snapshot — the binding constraint on a 5-year harvest |
| Line movement backtestable? | **YES** — this removes the "forward-test only" caveat from the V3 line-movement arm |