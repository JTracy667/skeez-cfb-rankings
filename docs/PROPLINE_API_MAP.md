# PropLine API Map — capability surface

**Maintainer:** CTO · **Created:** 2026-09-23
**Purpose:** full capability map of `api.prop-line.com`. Sibling of `docs/CFBD_API_MAP.md`.
Transport and availability only.

Regenerate: `python scripts/probe_odds_apis.py` + `python scripts/probe_odds_apis_detail.py`.

Verdicts: **VERIFIED-LIVE** = 200 with a non-empty body · **EMPTY** = 200/404 with empty body ·
**JUNK** = error or unusable · **UNPROBED** = not attempted.

---

## 1. Basics

| | |
|---|---|
| Base URL | `https://api.prop-line.com/v1` |
| Auth | **`X-Api-Key: <key>` header** |
| Quota signal | `x-ratelimit-limit` / `x-ratelimit-remaining` / `x-ratelimit-reset`, policy `5000;w=86400` |
| Observed | **4,595 of 5,000 remaining** (daily window) |
| Budget for this sweep | **~24 calls** |

### AUTH — verified by isolation, and it is not what our code assumes

| Form | Result |
|---|---|
| `Authorization: Bearer <key>` | **401** |
| `apiKey` **query param only** | **401** |
| **`X-Api-Key: <key>` header** | **200** |

`app.py` passes `apiKey` as a **query parameter**. Query-only auth returns 401 on `/events`, so
**the header must be what is actually authorising our production calls** — verify that
`_http_get_with_headers` sets `X-Api-Key` before trusting any new endpoint we add. If it does not,
every new PropLine call written in the existing style will 401.

### Correction to this document's own first pass

The first sweep recorded an "events (no key)" row as VERIFIED-LIVE, implying the endpoint is
public. **That probe was invalid** — it omitted the query param but still sent auth headers. The
endpoint **requires auth**. The row is withdrawn.

## 2. Endpoint table

| Endpoint | Params | Verdict | Measured |
|---|---|---|---|
| `/v1/sports` | — | **VERIFIED-LIVE** | 57 sports `{key,title,active}` |
| `/v1/sports/football_ncaaf/events` | header auth | **VERIFIED-LIVE** | **235** events, 114 KB |
| `/v1/sports/football_ncaaf/odds` | `markets=spreads,totals` | **VERIFIED-LIVE** | 233 events, **43.7 MB** |
| `/v1/sports/football_ncaaf/odds` | *(no markets)* | **VERIFIED-LIVE** | 235 events, 1.7 MB — returns **h2h only** |
| `/v1/sports/football_ncaaf/odds` | `markets=moneyline` | **EMPTY** (200, `[]`) | wrong key — see §3 |
| `/v1/sports/football_ncaaf/odds` | `markets=team_totals` | **EMPTY** (200, `[]`) | not offered |
| `/v1/sports/football_ncaaf/odds` | `markets=player_props` | **EMPTY** (200, `[]`) | not offered |
| `/v1/sports/football_ncaaf/events/{id}/best-line` | `markets=spreads,totals` | **VERIFIED-LIVE** | 18 books considered |
| `/v1/sports/football_nfl/odds` | `markets=spreads` | **VERIFIED-LIVE** | 69 events, 8.0 MB |
| `/v1/leagues` | — | **JUNK 404** | does not exist |
| `/v1/books`, `/v1/bookmakers` | — | **JUNK 404** | does not exist (books come inline on events) |
| `/v1/sports/football_ncaaf/odds/history` | — | **EMPTY 404** | no historical endpoint |
| `/v1/sports/football_ncaaf/history` | — | **EMPTY 404** | no historical endpoint |

### Payload-size trap

`markets=spreads,totals` returns **43.7 MB** for 233 events, against **1.7 MB** for the same
endpoint without `markets` — a ~25× difference. **This is the call production makes**, so the ~44 MB
body is pulled on every odds refresh.

**Corrected container context (verified 2026-09-23):** the live container is Cloudflare
`instance_type: "basic"` = **1 GiB**, not Render's 512 MB (Render is decommissioned). So this is a
**bandwidth and parse-cost** problem, not an OOM problem: ~44 MB on the wire per refresh, and
parsing it expands transiently well beyond the body size. It remains the single largest payload in
the system, and the honest fix if it ever bites is to narrow the request (per-event or
market-specific) rather than to raise memory.

## 3. Markets — the key is `h2h`, not `moneyline`

`markets=moneyline` returns an **empty list** while the moneylines are demonstrably present. The
correct key is **`h2h`** (the market object carries `"description": "Moneyline"`). Anyone writing
`markets=moneyline` gets a silent empty result rather than an error.

| Market | Key | Status |
|---|---|---|
| Moneyline | **`h2h`** | VERIFIED-LIVE (default market when `markets` is omitted) |
| Spreads | `spreads` | VERIFIED-LIVE (via `markets=`) |
| Totals | `totals` | VERIFIED-LIVE (via `markets=`) |
| Team totals | — | not offered (empty) |
| Player props | — | not offered (empty); schema carries `player_id`, so this looks plan-gated |

## 4. Bookmakers (18 observed)

`betmgm, betonlineag, betrivers, betus, bovada, draftkings, fanatics, fanduel, fliff, hardrock,
kalshi, lowvig, marathon, pinnacle, polymarket, polymarket_us, prophetx, rebet`

Notable: **`pinnacle`** and **`lowvig`** (sharp), **`prophetx`** (sharp market-maker), and
**`polymarket` / `polymarket_us` / `kalshi`** — prediction markets, a genuinely different price
source from sportsbook lines.

## 5. Field-level schema

**Event:** `id, sport_key, home_team, away_team, home_team_key, away_team_key, home_team_id,
away_team_id, home_team_logo_url, away_team_logo_url, commence_time, live, last_update,
bookmakers[], merged_from_event_ids`

- `home_team_id` / `away_team_id` carry **ESPN ids** (`espn.ncaaf:324`) — a ready crosswalk to any
  ESPN-keyed source. `events` also exposes `espn_event_id` and (for baseball) `mlb_game_pk`.
- `merged_from_event_ids` shows the provider merges duplicate listings.

**Bookmaker:** `key, title, last_update, markets[], book_event_id, link, app_link, pregame_only`

**Market:** `key, description, last_update, outcomes[], period, suspended_at, team`

**Outcome:** `name, description, price, point, book_outcome_id, book_updated_at, book_version,
payout_multiplier, dfs_odds_type, last_change_at, last_seen_at, liquidity, liquidity_updated_at,
line_gap, outcome_id, player_id`

Fields we are **not** currently reading that look useful:

- **`last_change_at` / `last_seen_at`** — when this price last moved, and when we last saw it.
  This is the closest thing PropLine has to movement data.
- **`line_gap`** — present on every outcome; plausibly the gap vs consensus. **Semantics UNPROBED.**
- **`liquidity` / `liquidity_updated_at`** — exchange-style depth, populated on exchange books.
- **`suspended_at`** (market level) and **`pregame_only`** (book level) — availability state we
  currently ignore, which matters for not betting into a suspended market.

## 6. `/best-line` response (full shape)

Keys: `id, sport_key, home_team, away_team, commence_time, books_considered[], lines[],
redacted, upgrade_url`

```
lines[] = { market_key, description, point,
            sides: { "<team>": { best:   { book, book_title, price, last_update, link, app_link,
                                           liquidity, liquidity_updated_at },
                                 all_prices: [ ... ] } } }
```

Measured: `books_considered` listed **18** books for a single event. The `best` object is the
consensus-best price with book attribution; `all_prices` carries the rest.

**`redacted` + `upgrade_url` are present in the payload** — the provider signals that some data is
withheld on the current plan. The semantic of `redacted` here is **UNPROBED**; do not assume a
`best` price is the true market best until we know what is being withheld.

## 7. Explicit historical-availability answer

| Question | Answer |
|---|---|
| Historical odds endpoint? | **NO** — `/odds/history` and `/history` both 404 |
| Open/close pairs stored? | **NO** — no open/close fields anywhere in the schema |
| Movement data at all? | Partial: `last_change_at` + `last_seen_at` per outcome give the **timestamp of the most recent move**, not a series |
| Line movement backtestable from PropLine? | **NO — forward-test only.** We would have to record snapshots ourselves from now on |
| Past seasons' odds? | **Not available from this API.** For historical backtesting use The Odds API's historical endpoint (`docs/ODDS_API_MAP.md` §3) |

**Implication for the V3 line-movement arm:** PropLine cannot back it. The Odds API history can.
If we want PropLine's sharper book mix in the backtest, the only route is to start snapshotting
now and treat it as forward evidence.