# CFBD API Map — how to poll every endpoint we use

**Maintainer:** CTO · **Created:** 2026-09-22
**Purpose:** one place that answers *"how do I poll this endpoint, and what does it cost?"*
for every CFBD endpoint this project calls. The field-level semantics stay Research's
territory — this map covers **transport**: params, payload size, ETag/conditional
behaviour, quota cost, and the traps.

**Regenerate the measured table** (rows/sizes/ETag/304 support are observed, not assumed):

```bash
python scripts/probe_api_surface.py     # writes data/api_surface_map.json
python scripts/probe_api_surface.py --help 2>/dev/null || true
```

Verified against the live API on 2026-09-22 (CFBD v2, `year=2026`).

---

## 1. Basics

| | |
|---|---|
| Base URL | `https://api.collegefootballdata.com` |
| Auth | `Authorization: Bearer $CFBD_API_KEY` (key in repo `.env`) |
| Quota signal | response header `X-CallLimit-Remaining` |
| Observed remaining | ~25,750 of a ~30,000 allowance |
| Our usage | tiny; the budget is not the constraint — correctness and staleness are |

**Send a real `User-Agent`.** A header-poor client can be rejected (1010) by the CDN in
front of our own Worker; do the same for CFBD probes so failures are real.

---

## 2. Measured surface (2026-09-22)

`304?` = does `If-None-Match` with that endpoint's own ETag return 304.
`304cost` = calls consumed by that 304 (measured from `X-CallLimit-Remaining`).

| Endpoint | Extra params | Rows | Bytes | ETag | 304? | 304cost |
|---|---|---|---|---|---|---|
| `teams` | — | 682 | 959,950 | yes | yes | 1 |
| `ratings/sp` | — | 139 | 72,267 | yes | yes | 1 |
| `ratings/elo` | — | 138 | 9,543 | yes | yes | 1 |
| `ratings/fpi` | — | 138 | 42,763 | yes | yes | 1 |
| `ratings/srs` | — | **0** | 2 | yes | yes | 1 |
| `records` | — | 682 | 356,643 | yes | yes | 1 |
| `recruiting/teams` | — | 221 | 13,280 | yes | yes | 1 |
| `talent` | — | 138 | 6,832 | yes | yes | 1 |
| `stats/season` | — | 8,487 | 896,086 | yes | yes | 1 |
| `stats/season/advanced` | — | 138 | 337,440 | yes | yes | 1 |
| `ppa/teams` | — | 138 | 54,694 | yes | yes | 1 |
| `ppa/players/season` | — | 4,581 | 1,862,963 | yes | yes | 1 |
| `player/returning` | — | 136 | 43,892 | yes | yes | 1 |
| `games` | — | 3,679 | **2,756,460** | yes | **NO** | — |
| `lines` | `week` | 71 | 43,453 | yes | yes | 1 |
| `drives` | `week`, `team` | 0 | 2 | yes | yes | 1 |
| `roster` | `team` | 128 | 38,067 | yes | yes | 1 |
| `games/weather` | `week`, `team` | 1 | 436 | yes | yes | 1 |

**18/18 endpoints return an ETag. 17/18 honour it.** The exception is the single
largest payload we pull.

---

## 3. Polling recipes

### 3a. Conditional poll (the default — works for 17 of 18)
```bash
# store the ETag from the last 200; send it back; 304 == unchanged
curl -s -D - -o body.json -H "Authorization: Bearer $CFBD_API_KEY" \
     -H 'If-None-Match: W/"..."' "https://api.collegefootballdata.com/ratings/sp?year=2026"
```
- `304` → nothing changed. **Still costs 1 call** — conditional polling saves the
  **body, the parse, the recompute and the DB write, NOT quota.** Do not claim a quota
  win it does not deliver.
- The conditional-200 body is **byte-identical** to a plain GET (verified), so this
  route returns the *same data*, not a truncated variant.

### 3b. `games` — canonicalise, then hash (the ETag is useless here)
The `/games` **row order is not stable**, so its ETag changes on every request even
when the content is identical. Measured:

```
call 1  ETag W/"2a0f6c-wLnd6l+3..."   sha256 498f1485eca0092d   2756460 bytes
call 2  ETag W/"2a0f6c-kk16c/ki..."   sha256 735f0f2e26e906fc   2756460 bytes
call 3  ETag W/"2a0f6c-bR7mCz1F..."   sha256 38b57c7540f4784e   2756460 bytes

same id SET: True     same id ORDER: False
content identical once sorted by id: True
canonical hash (sorted by id, sorted keys): 08de10a063af3b56 == 08de10a063af3b56
```
**So:** never trust `/games`' ETag as a change signal, and never diff it positionally —
that reports ~3,238 "changed" rows when nothing changed. Canonicalise first:

```python
rows = json.loads(body)
canon = json.dumps(sorted(rows, key=lambda g: g.get("id") or 0), sort_keys=True)
changed = hashlib.sha256(canon.encode()).hexdigest() != last_hash
```
This is the one endpoint where we always pay the full 2.76 MB transfer; the win is
skipping the parse + downstream write when the canonical hash is unchanged.

### 3c. Pacing — poll in the release window, not uniformly
CFBD drops ratings at an unpredictable time **between Sunday night and Wednesday**, so
the cadence exists to *catch the release*, not because values change 6-hourly. An
age-based freshness check cannot detect a release (data can be 2h old and still miss
it). Window-bounded probing spends zero calls outside it:
`scripts/probe_ratings_etag.py` (cron `cto-cfb-ratings-etag-probe`).

---

## 4. Known traps

1. **304 still costs a call.** Budget it as a call, always.
2. **`/games` ETag is volatile by design** (row order). Canonicalise.
3. **`ratings/srs?year=2026` returns an empty array.** The app then falls back to the
   **previous season** (2025), which is why live SRS values exist. The same
   fallback pattern covers `ratings/elo` and `talent`. Consequence: any per-input
   freshness gate must be **per-input cadence-aware**, not a single age threshold — an
   input that is legitimately a season old will otherwise trip it permanently.
4. **Empty results are not errors.** `drives` (week 4, pre-game) and `ratings/srs`
   return `[]` with HTTP 200. Treat 200 + empty as "no data yet", and check whether a
   caller is silently operating on a fallback.
5. **Param-dependent sizes.** Some endpoints need `week` and/or `team`; calling without
   them returns 200 with an empty array rather than an error — a silent wrong answer.
6. **Big payloads**: `games` 2.76 MB, `ppa/players/season` 1.86 MB, `teams` 960 KB,
   `stats/season` 896 KB. A full ingest is ~7 MB.

---

## 5. Provenance / how to update this map

- Rows/sizes/ETag/304 columns come from `scripts/probe_api_surface.py` (read-only,
  ~36 calls). Re-run it after any CFBD change and refresh §2.
- The list of endpoints is derived from the code, not from documentation:
  `grep -rhoE 'cfbd_get\("[a-z0-9/_]+"' *.py scripts/*.py | sort -u`
  plus direct `{CFBD_BASE}/...` builds. If you add a call site, add it here.
- Transport facts only. **Do not** record field meanings here — that is Research's map.