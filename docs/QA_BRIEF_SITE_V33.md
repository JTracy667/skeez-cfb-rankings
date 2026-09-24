# QA BRIEF — FULL SITE PASS: skeezcfb-rankings.com

**Asked by:** Jeff, 2026-09-23
**Owner of the site:** CTO
**Target of this pass:** the whole public surface (4 pages + 31 API routes), on
build **v33** (v32 + the `/api/health` `model_version` field; no model behaviour change).
**Status when written:** v33 was mid-deploy. Do not start until CTO confirms "v33 verified live".

---

## 0. Read this before you touch anything

**1. Deployment window = hands off.** After a `wrangler deploy`, the warm container can keep
serving the PREVIOUS image for up to ~20 minutes (sleepAfter is now 5m, the old instance still
needs its own idle-out). Polling the site during that window keeps the old instance warm and
*stretches the window out*. If `GET /api/health` shows a `build` that is not the tag CTO named,
STOP probing for 15+ minutes. Never run a tight polling loop against this site.

**2. `build` is not proof of live code.** The Worker passes `BUILD_TAG` to whatever instance
answers, including a stale one. Since v33 the code itself reports `model_version`
(`composite_version()`); on v33 the pair must read:

```
"build": "v33"          <- what the Worker labelled the instance
"model_version": "c4..." <- computed BY THE RUNNING CODE (this is the real proof)
```

For v33 the expected `model_version` is **cc4e71b6d16** (same config as v32 — v33 only adds
the field). Cross-check it against D1: today's `rankings_daily` rows carry the same string.

**3. Payload → render → read. A 200 with rows is not an accepted result.** For every page,
confirm the API returns the field, the served JS actually references it, and the DOM shows it.
A contract field that the API promises and the UI never reads is dead data — file it.

**4. Every request resets the container's idle timer.** Record the timestamp of every request
you make. Space exploratory probes minutes apart.

---

## 1. Baseline — captured 2026-09-23 13:28 PT (v32; must be UNCHANGED on v33)

```
GET /api/health      build=v32, teams=25, cache_ttl=300, degraded=false, degraded_sources=[]
                     budget: cfbd 14.78% (4435/30000), odds 32.06% (6412/20000),
                             propline 26.7%, d1 0.33%
GET /api/rankings    teams=25, ranks 1..25 contiguous, composites strictly descending,
                     elo_nulls=0, sp_zero=0
                     input_vintages = {talent: 2026, srs: 2025, elo: 2026}
                     top 5: Georgia 83.1, Ohio State 81.0, Oregon 78.0, Texas 76.0, Notre Dame 75.3
D1 rankings_daily    date=2026-09-23 -> 25 rows, model_version=cc4e71b6d16
                     date=2026-09-22 -> 25 rows, model_version=ca2f071afda  (pre-decay code)
```

Any movement in the top-5 composites on v33 is a **finding**, not noise: v33's only code delta
is a new health field, and today is week 4 (talent decay is a deliberate no-op through week 4).

---

## 2. Pages in scope (enumerated from the Dockerfile COPY line — not from memory)

`Dockerfile:22` is the page manifest: `index.html analytics.html schedule.html win_totals.html`.
**A new page that is not added to that COPY line 500s in prod** — if you see a page linked in the
nav that is missing from that line, that is a P1 finding. Verify via `grep -n COPY Dockerfile`.

### 2a. `/` — Rankings board (`index.html`)
- Endpoints: `GET /api/rankings` (plus `/api/rankings/search`, `/api/rankings/{rank}`,
  `/api/rankings/conf/{conf}` — exercise each).
- **Invariant (Jeff corrected this twice):** the table loads sorted by composite rank, ALWAYS.
  AP and Coaches `<th>` must have **no `data-sort` attribute** — reference-only, never sortable.
  Sortable columns are rank / team / conf / movement / record / streak / composite / points.
  Confirm by grepping the SERVED page: `curl -s <site>/ | grep -o 'data-sort="[a-z]*"'`
  — `ap_rank` / `coaches_rank` must not appear as sort keys.
- Assert: `tbody#rankingsBody` has exactly 25 rows; row order matches API order; rank cells read
  1..25; no row renders `undefined` / `NaN` / blank composite.
- Search box (`#searchBox`) must filter, not blank the table.
- Clicking the composite header must re-sort by the numeric value, not the string.

### 2b. `/analytics` (`analytics.html`)
- Endpoints: `GET /api/analytics`, `GET /api/projections`, `GET /api/odds`.
- Anchor: analytics payload covers **685 teams** (all divisions, FBS + FCS) — a payload in the
  tens is a truncation bug. `#sourceBadge` must read the real team count.
- Tabs (`#spplus #fpi #srs #elo #recruiting #coaching #portal #composite #projections`) must each
  render content on click, from data already fetched — clicking a tab must NOT fire a new network
  call (the page deliberately does not POST on load; that hammered CFBD).
- **Known expectation:** there is no manual refresh button on this page any more. `POST
  /api/analytics/fetch` is admin-gated and ops-only. If you find UI that claims to refresh and
  silently 401s, that's a finding.
- Composite tab values must equal `/api/rankings` composites for the same team (one source of
  truth — a mismatch between pages is P1).

### 2c. `/schedule` (`schedule.html`)
- Endpoints: `GET /api/schedule/weeks`, `GET /api/schedule/current-week`, `POST /api/schedule/fetch?week=N`
  (deliberately NOT token-gated — the page calls it on load; it is throttled to a 300s payload),
  `GET /api/schedule`, `GET /api/best-bets`, `GET /api/best-bets/record`, `GET /api/record`,
  `GET /api/line-movements?hours=168`, `GET /api/injuries`, plus `GET /api/odds` /`/api/projections`.
- **Schedule data source is CFBD `/games`. The ESPN scoreboard truncates to 25 events/week and
  must NEVER be wired back in.** Verify by grepping app.py for `espn` — it must be absent from the
  request path. `data/espn_*.json` are legacy artifacts from one-off scripts; if any app.py code
  path reads them, that is a P1 finding.
- Week selector (`#weekSelect`) must repopulate the board per week; `#weekBadge` must match.
- Cards (`#summaryRow #atsCard #suCard #totalCard #starsCard #bestBets`) must all populate; each
  best-bet row must show a pick and a numeric edge — not `undefined`.
- Injury modal: click a flagged team -> `#injuryModalBody` populates; close works; a team with no
  injuries must not open an empty modal.
- **Numbers discipline:** projected scores are passed through raw. A projected score that looks
  clamped/smoothed (e.g. nothing ever outside a narrow band, a 70-0 projection squashed to 45-10)
  is a P1 finding against the model contract — Jeff's rule is "the projection displays exactly what
  the model produced".

### 2d. `/win-totals` (`win_totals.html`)
- Endpoint: `GET /api/win-totals`.
- Assert `#wtBody` row count == the payload's row count (report the number; 0 rows or a count
  mismatch is P1). `#metaBadge` must show a real vintage/updated stamp, not blank.
- Sortable columns here are fine (5 sort refs) — but confirm the row ORDER controls work and that
  sorting does not lose rows.
- Win totals must agree with the schedule page where both display the same team/line.

---

## 3. API matrix (31 routes)

Public GETs: `/api/rankings`, `/api/rankings/search`, `/api/rankings/conf/{conf}`,
`/api/rankings/{rank}`, `/api/health`, `/api/budget`, `/ping`, `/api/analytics`,
`/api/analytics/pull-status`, `/api/schedule`, `/api/schedule/weeks`,
`/api/schedule/current-week`, `/api/odds`, `/api/projections`, `/api/injuries`, `/api/record`,
`/api/best-bets`, `/api/best-bets/record`, `/api/line-movements`, `/api/win-totals`.

**Admin-gated POSTs (must carry `X-Admin-Token`):** `/api/rankings/refresh`, `/api/analytics/fetch`,
`/api/analytics/refresh-if-due`, `/api/schedule/update`, `/api/injuries/sync`,
`/api/injuries/override`, `/api/record/ingest`, `/api/record/repair-ats`, `/api/refresh`.

**Not gated on purpose:** `POST /api/schedule/fetch` (the Schedule page calls it on load; it is
throttled to a 300s cached payload instead of token-gated — a public page cannot hold a secret).

Auth semantics to verify (all token-free, do NOT send a token):
- Every gated POST without a token -> **401** (gate is UP).
- The gate **fails closed**: it returns **503 only when `ADMIN_TOKEN` is unset**. 503 on a gated
  route means the secret is missing in the environment — P1, escalate.
- `POST /api/schedule/fetch` without a token -> 200 (or a throttled 200), never 401.
- Docs surface: `/docs`, `/redoc`, `/openapi.json` must be **404/405** (deliberately disabled).
- Baseline security headers present on a normal GET.
- Upstream contract: `GET /` and every page must include the security header set and no server
  version banner.

Edge cases worth a probe each: `/api/rankings/999` (out of range), `/api/rankings/conf/XX`
(bogus conf), `/api/rankings/search?q=` (empty), `/api/line-movements?hours=0` and a huge value.

---

## 4. Standing invariants — check each, file the violation

1. **Rankings sort:** API composites strictly descending, ranks 1..N contiguous, and the page
   loads in that order. AP/Coaches never sortable.
2. **No ESPN schedule source** (see 2c).
3. **D1 write discipline:** today's `rankings_daily` rows == the live board's team count, all
   under the same `model_version` the health endpoint reports. A day where rows exist under two
   different model_versions is a finding.
4. **Honest degradation:** `/api/health` `degraded` / `degraded_sources` must reflect real quota
   exhaustion, and `budget` percentages must move sensibly. A page that shows a stale dataset while
   health says `degraded: false` is a finding.
5. **Cache behaviour:** `/api/rankings` is served from a 300s cache; a value must not change
   between two reads 30s apart, and must be self-consistent within one payload (composite vs the
   ranking built from it).
6. **Composite input freshness:** every metric the composite actually uses must return live data
   (this was the v30/v31 fix). If `/api/rankings` rows contain nulls or flat 50s for an input, say
   which field and how many rows.

---

## 5. Regression probes for the most recent changes (do these explicitly)

- **v32/V6.2 talent decay.** Today is week 4 -> decay scale is 1.00 -> the board must be
  numerically identical to the 13:28 PT baseline above. The published curve is wk4 1.00, wk5 0.80,
  wk6 0.60, wk7+ 0.50. From week 5 onward the top composites SHOULD drift; flag any claim that the
  board "hasn't changed" once we are past week 4, and flag any change while still in week 4 as P1.
- **`model_version` marker (new in v33).** Health must return it; it must equal today's D1
  `rankings_daily.model_version`; it must NOT equal yesterday's (`ca2f071afda`).
- **Refresh bug class (fixed 2026-09-20).** `POST /api/rankings/refresh` (with token) then re-read
  `/api/rankings`: `elo_nulls` and `sp_zero` must both be **0**. The old bug rebuilt the board from
  an ESPN poll stub and cached a 25-field payload for 300s.
- **FCS composite.** FCS opponents carry fitted composites (33.0 ranked / 15.0 unranked / 19.0
  fallback) driven by a live Massey FCS rank map, NOT a flat constant. **How to read this
  correctly:** the map covers ~128 FCS teams and the tiers are coarse, so several unranked FCS
  opponents legitimately share the value **15.0** in one week — that is the designed "unranked
  FCS" tier, not the old bug. Ranked teams must differ: South Dakota State (rank 14) → 33.0 while
  Howard (rank 98) → 15.0. File a finding only if a KNOWN TOP-25 FCS team (SDSU / NDSU / Montana
  State class) shows 15.0, or if values are flat because the rank map failed to load.

---

## 6. Do NOT

- Do not poll during a deploy window (section 0).
- Do not touch D1 with range deletes: CFBD `game_id`s are ~40xxxxxxx, so a low numeric threshold
  (`WHERE game_id >= 9999000`) wipes the table. Read-only D1 checks only.
- Do not change weights, projections, or betting rules to make a test pass. Report, don't fix.
- Do not file a screenshot-only content/typo issue — confirm against the served source first.
- Do not send an admin token to a public page.

## 7. What to hand back (receipts)

Per page: URL, HTTP status **with the exact timestamp of the request**, the row count you counted
in the DOM, and the payload→render→read result per contract field. Per API route: status code +
top-level keys + row count. Per finding: severity, exact request (curl), observed vs expected,
and whether it is reproducible on a second spaced-out attempt.

**Severity:** P1 = public page 500s / blank board / wrong order / wrong data source / gate open;
P2 = field promised but not rendered, stale data shown as fresh, one page disagreeing with another;
P3 = copy, layout, or cosmetic.

CTO acts on P1 immediately; P2 goes into the next patch; P3 into the next cosmetic pass.