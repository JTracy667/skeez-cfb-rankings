# Analytics serve contract

Written for QA remediation §3/§4. The code this describes is in `app._served_analytics()`;
the state it reports is `app._ANALYTICS_SERVE`, exposed as `serve` on `/api/analytics`.

## 1. The fallback chain (what answers a request)

In order, first match wins:

1. **Operator opt-out** — `ANALYTICS_FROM_D1=0` → the disk file. Zero D1 reads.
   Reported: `disk (ANALYTICS_FROM_D1=0)`, degraded.
2. **Verified publication** — `app_state` key `analytics_publication:<season>` exists and the
   rows at its `stamp` are readable → those rows. Reported: `d1`, not degraded.
3. **Documented disk fallback** — no verified publication (missing, malformed, or nothing
   readable at its stamp) → the image-baked `data/cfbd_analytics.json`, reported as DEGRADED.
   The marker no longer suppresses this: a missing marker is exactly when the fallback is
   needed.
4. **Unavailable** — no publication and no disk fallback → empty, reported as `empty`,
   degraded. The `/api/analytics` route then attempts a live fetch, which is the only path
   that spends metered quota.

A **D1 read failure** is a variant of 3 (`disk (d1 read failed)`, degraded) — never a 500.

## 2. The identity / numeric contract

- **Numerics** come only from the published snapshot (or, degraded, the disk fallback).
  No numeric value is ever invented: a team with no covered metrics is served with those
  metrics ABSENT, never as 0.
- **Identity fields** (name, mascot, conference, emoji, streak/confidence) are joined by
  **team name**, which is the stable key across all three sources.
- **The live identity key wins** for identity fields: it is refreshed by the pull and is not
  frozen at the publication stamp. The marker's `identity` blob is an audit/rollback record.
- **The published snapshot defines the numeric universe.** A team present in the live
  identity key but absent from the snapshot is served **identity-only** — it renders, and it
  carries no numerics.
- **A mixed universe is reported, never hidden.** If numeric rows carry names outside the
  published universe, the serve is marked degraded (`d1 (mixed universe)`) with the count.

## 3. What a consumer may rely on

- HTTP 200 does not mean "verified". Read `serve.source` and `serve.degraded`.
- `serve.degraded: true` means the numbers came from a fallback, or the serve mixed
  snapshots. A monitor should alarm on it; a board builder must refuse it (see 4).
- `serve.source` is the PATH (`d1`, `disk (...)`, `empty`); the payload's `source` field is
  the DATA PROVIDER (`cfbd`). They are different questions.

## 4. Building a board for D1

`_VERIFIED_ONLY` is set while `d1_write_path.daily_rankings()` builds the once-daily board.
In that window the disk fallback is refused (`app._verified_analytics_only()`), because a
board becomes the system of record: baking a degraded fallback into D1 would make the
staleness permanent and invisible. A board is built from a verified publication or not at all.