# QA RECEIPTS — credentialed probes, v33 (run by CTO, not an independent check)

**For:** QA pass on `docs/QA_BRIEF_SITE_V33.md`
**Run by:** CTO, 2026-09-23 (times PT)
**Why CTO ran it:** these probes are the only ones needing a credential
(`X-Admin-Token`, D1 read). The rest of the brief needs no auth and is still yours.
**Status:** external evidence, NOT independent verification. Reproduce anything you
doubt; you now have everything needed to do so without holding a secret.

---

## 1. Auth-gate semantics (brief §3)

Run 2026-09-23 ~14:35 PT, all token-free unless stated:

```
POST /api/rankings/refresh  (no token)          -> 401
POST /api/rankings/refresh  (bogus token)       -> 401
POST /api/schedule/fetch?week=4 (no token)      -> 200   <- ungated by design (300s throttled payload)
```

Verdict: gate is UP, not failing closed (a fail-closed gate would answer 503).
A 503 on any gated route means `ADMIN_TOKEN` is missing in the environment — P1.

## 2. Gated refresh — the 2026-09-20 regression probe (brief §5, item 3)

```
POST /api/rankings/refresh   X-Admin-Token: <ADMIN_TOKEN>   -> 200
body: {"status":"refreshed","source":"espn","teams":25}
```

Then `GET /api/rankings` immediately after:

```
                 rows  elo_nulls  sp_zero  flat-50  sorted  keys/row  updated
pre-refresh       25       0         0        0      yes      82      20:47:15Z
post-refresh      25       0         0        0      yes      82      22:34:58Z
board identical to pre-refresh: True
top 5 unchanged: Georgia 83.1, Ohio State 81.0, Oregon 78.0, Texas 76.0, Notre Dame 75.3
input_vintages: {talent: 2026, srs: 2025, elo: 2026}
```

**Verdict: PASS — the old bug did NOT reproduce.** The fixed path rebuilds from the
CFBD seed; rows still carry the full 82-field payload and every CFBD metric is present
(the old failure mode was a 25-field ESPN-stub payload with `elo` null and `sp_plus` 0).

### ⚠ One label to NOT file as a bug
The refresh response says `"source":"espn"`. That is the **AP/Coaches poll leg** of the
rebuild (poll ranks are the one thing ESPN supplies), not the board's data source — the
payload above proves the CFBD metrics survive. It is a misleading *label* on a healthy
code path. CTO will relabel it (`cfbd+poll`) in the next patch; treat it as P3 copy at
most, not a data-source finding.

## 3. D1 cross-checks (brief §4, item 3)

Read-only `wrangler d1 execute cfb-history --remote`:

```
rankings_daily
  date 2026-09-23   model_version cc4e71b6d16   25 rows   <- matches /api/health model_version
  date 2026-09-22   model_version ca2f071afda   25 rows   <- yesterday (pre-decay code)

model_predictions
  cd7e863a1bc   71 rows
  cc4e71b6d16   71 rows
  ca2f071afda   71 rows
```

Verdict: PASS — today's 25 rows == the live board's 25 teams, all under the same
`model_version` the health endpoint reports. No day carries rows under two versions.
Three `model_predictions` versions coexist by design (the table is keyed
`game_id + model_version`, so each day's snapshot is preserved rather than overwritten).

## 4. Page status at verification (13:57 PT)

```
/api/health 200 (build v33, model_version cc4e71b6d16, teams 25, degraded false)
/ 200   /analytics 200   /schedule 200   /win-totals 200
```

## 5. What is still yours (unchanged by this receipt)

Everything in the brief that is uncredentialed: the DOM/payload→render→read contracts on
all four pages, the AP/Coaches `data-sort` check, the analytics 685-team anchor, schedule
cards + injury modal, win-totals row count, the `/docs` `/redoc` `/openapi.json` 404s,
edge cases, and the decay behaviour once we pass week 4.

If a probe needs a credential mid-pass, hand it back instead of working around it —
there is a clean path for each one.