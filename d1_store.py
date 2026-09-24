#!/usr/bin/env python3
"""d1_store.py — thin wrapper over the Cloudflare D1 REST API (Phase 2).

Purpose: persist the app's history (odds snapshots, stat observations, daily
rankings, closing lines, model predictions) into D1 `cfb-history`.

SECURITY (deliberate): the token is read from `CF_D1_TOKEN` FIRST. Provision a
Cloudflare token scoped to D1 (account-level "D1:Edit") for the container env.
Do NOT hand the app the broad zone token (DNS:Edit) — `CLOUDFLARE_API_TOKEN` is
a local-dev fallback only.

PERFORMANCE: writes are BATCHED — one HTTP request per ~400 rows (multi-row
VALUES / IN-list), never one request per row. A 190K-row backfill is ~500
requests, not 190K.

WRITE ACCOUNTING (CEO directive 2026-09-21): the counter of record is the D1 API
RESPONSE itself (`meta.rows_written` / `meta.changes`). We never infer a write
from the local side: a batch the API reports as 0 rows written raises
ConfirmedWriteError, so a "done" chunk can never mean "wrote nothing".

Additive + safe: nothing here runs until a caller imports it. Callers must fall
fall back to local cache on failure (D1_RISK_REGISTER B4). The budget guard enforces
the D1 row-WRITE cap: the account is on Workers PAID (verified 2026-09-21 against the
Cloudflare API: /accounts/<id>/subscriptions -> rate_plan.id == "workers_paid"), i.e.
50,000,000 rows written/MONTH — there is no 100K/day tier limit. D1_DAILY_WRITE_CAP
defaults to 2,000,000/day, which is pure pacing/runaway protection with huge headroom
inside the 50M monthly pool; the ledger still counts CONFIRMED writes either way.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "90c2c31beec12cb7de1c249ade1eb773")
D1_DB_ID = os.environ.get("CF_D1_DB_ID", "c3ec3149-cc85-483b-b727-5a18e3d5a1b9")
CHUNK = 400
LAST_META: dict = {}   # meta of the most recent D1 query (carries rows_written)


def _token() -> str:
    tok = os.environ.get("CF_D1_TOKEN") or os.environ.get("CLOUDFLARE_API_TOKEN") or ""
    if not tok:
        raise RuntimeError("no D1 token: set CF_D1_TOKEN (scoped D1:Edit)")
    return tok


def _api_url() -> str:
    return (f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}"
            f"/d1/database/{D1_DB_ID}/query")


# --------------------------------------------------------------- budget guard
_LEDGER = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                       "hermes", "d1_write_ledger.json")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load_ledger() -> dict:
    try:
        with open(_LEDGER, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("date") == _today():
            return d
    except Exception:
        pass
    return {"date": _today(), "rows_written": 0}


def _save_ledger(d: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_LEDGER), exist_ok=True)
        with open(_LEDGER, "w", encoding="utf-8") as f:
            json.dump(d, f)
    except Exception:
        pass


class BudgetExceeded(RuntimeError):
    pass


class ConfirmedWriteError(RuntimeError):
    """D1 reported 0 rows written for a batch that had rows to write."""


def ledger_written() -> int:
    return _load_ledger()["rows_written"]


def assert_headroom(n_rows: int, daily_cap: int | None = None) -> None:
    """Refuse to START a write that would exceed the daily cap (check only)."""
    cap = daily_cap if daily_cap is not None else int(os.environ.get("D1_DAILY_WRITE_CAP", "2000000"))
    led = _load_ledger()
    if led["rows_written"] + n_rows > cap:
        raise BudgetExceeded(f"D1 daily write budget: {led['rows_written']}+{n_rows} > {cap}")


def commit_writes(n_rows: int, meter: bool = True) -> None:
    """Record ACTUAL confirmed writes — taken from the D1 response's meta.rows_written,
    never a local guess (CEO: counter must assert on the API response).

    Also mirrors the number into the standing budget ledger (Phase 3.5) so the D1
    burn is answerable alongside the API burns. `meter=False` is used for the
    ledger's OWN writes to api_usage — counting the meter would make it feed itself.
    """
    led = _load_ledger()
    led["rows_written"] += int(n_rows)
    _save_ledger(led)
    if meter and int(n_rows) > 0:
        try:
            import budget  # noqa: PLC0415 — lazy: avoids a circular import
            budget.record_d1_rows(int(n_rows))
        except Exception:  # noqa: BLE001 — metering must never break a write
            pass


def write_budget_ok(n_rows: int, daily_cap: int | None = None) -> None:
    """Check + optimistically commit (for simple callers with no meta to read)."""
    assert_headroom(n_rows, daily_cap)
    commit_writes(n_rows)


def confirmed_writes(meta: dict | None) -> int:
    """Rows the D1 API ITSELF reports as written for one statement.

    NO local fallback by design: if the API does not say it wrote rows, it did not
    write rows. This is what makes the counter trustworthy.
    """
    if not meta:
        return 0
    v = meta.get("rows_written")
    if v is None:
        v = meta.get("changes")
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


# ------------------------------------------------------------------------- core
def query_full(sql: str, params: list | None = None, timeout: int = 60) -> tuple[list[dict], dict]:
    """Run one SQL statement. Returns (rows, meta) — `meta` is the D1 API's OWN
    accounting for the statement (changes / rows_written / rows_read)."""
    body = json.dumps({"sql": sql, "params": params or []}).encode()
    req = urllib.request.Request(_api_url(), data=body, method="POST",
                                 headers={"Authorization": f"Bearer {_token()}",
                                          "Content-Type": "application/json"})
    last = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = json.loads(r.read())
            break
        except urllib.error.HTTPError as e:
            body = e.read()[:300]
            if e.code >= 500 or e.code == 429:
                last = f"D1 HTTP {e.code}: {body!r}"
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f"D1 HTTP {e.code}: {body!r}") from e
        except Exception as e:  # noqa: BLE001 — transient resets on long runs
            last = repr(e)
            time.sleep(2 * (attempt + 1))
            continue
    else:
        raise RuntimeError(f"D1 unreachable after retries: {last}")
    if not payload.get("success"):
        raise RuntimeError(f"D1 error: {payload.get('errors')}")
    global LAST_META
    res = payload.get("result") or []
    meta = (res[0].get("meta") or {}) if (res and isinstance(res[0], dict)) else {}
    LAST_META = meta
    return ((res[0].get("results") if res else []) or []), meta


def query(sql: str, params: list | None = None, timeout: int = 60) -> list[dict]:
    """Back-compat wrapper: rows only."""
    rows, _ = query_full(sql, params, timeout)
    return rows


def _chunks(rows: list, n: int = CHUNK):
    for i in range(0, len(rows), n):
        yield rows[i:i + n]


def _upsert(table: str, cols: list[str], rows: list[dict],
            conflict: list[str] | None, update: list[str] | None) -> int:
    """One batched multi-row INSERT per chunk (idempotent when conflict given).
    D1 caps bound parameters at 100/query -> chunk by column count.
    Returns CONFIRMED rows written (from the D1 response), not planned rows."""
    n = 0
    for part in _chunks(rows, max(1, 100 // len(cols))):
        assert_headroom(len(part))
        ph = ",".join("(" + ",".join("?" * len(cols)) + ")" for _ in part)
        sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES {ph}"
        if conflict and update:
            sql += (f" ON CONFLICT({','.join(conflict)}) DO UPDATE SET "
                    + ",".join(f"{c}=excluded.{c}" for c in update))
        params = [v for r in part for v in (r.get(c) for c in cols)]
        _, meta = query_full(sql, params)
        confirmed = confirmed_writes(meta)
        if confirmed <= 0:
            raise ConfirmedWriteError(
                f"{table}: D1 confirmed 0 rows written for a {len(part)}-row batch (meta={meta})")
        commit_writes(confirmed, meter=(table != "api_usage"))
        n += confirmed
    return n


def _replace_by(table: str, key_cols: list[str], cols: list[str], rows: list[dict]) -> int:
    """For tables with no unique index: delete the chunk's keys, then batched insert.
    D1 caps bound parameters at 100/query -> chunk by the widest column list.
    Returns CONFIRMED rows written (delete + insert), from the D1 responses."""
    n = 0
    step = max(1, 100 // max(len(cols), len(key_cols)))
    for part in _chunks(rows, step):
        assert_headroom(len(part))
        keys = sorted({tuple(r.get(c) for c in key_cols) for r in part})
        ph = ",".join("(" + ",".join("?" * len(key_cols)) + ")" for _ in keys)
        _, meta_del = query_full(f"DELETE FROM {table} WHERE ({','.join(key_cols)}) IN ({ph})",
                                 [v for k in keys for v in k])
        ph2 = ",".join("(" + ",".join("?" * len(cols)) + ")" for _ in part)
        _, meta_ins = query_full(f"INSERT INTO {table} ({','.join(cols)}) VALUES {ph2}",
                                 [v for r in part for v in (r.get(c) for c in cols)])
        confirmed = confirmed_writes(meta_del) + confirmed_writes(meta_ins)
        if confirmed <= 0:
            raise ConfirmedWriteError(
                f"{table}: D1 confirmed 0 rows written for a {len(part)}-row replace "
                f"(del={meta_del} ins={meta_ins})")
        commit_writes(confirmed)
        n += confirmed
    return n


# --------------------------------------------------------- typed row helpers
def upsert_teams(rows: list[dict]) -> int:
    return _upsert("teams", ["team_id", "name", "abbr", "conference", "first_season"],
                   rows, ["team_id"], ["name", "abbr", "conference"])


API_USAGE_COLS = ["bucket", "period", "source", "calls", "provider_remaining",
                  "provider_limit", "provider_used", "updated_at"]


def upsert_api_usage(rows: list[dict]) -> int:
    """Persist the standing budget ledger (D1_CHECKLIST Phase 3.5).

    The ledger of record lives in D1, not a file, because the Cloudflare
    container filesystem is ephemeral — a file-only ledger silently resets on
    every instance recycle and under-reports the day's burn.

    Tolerant of a true no-op: re-writing an identical value is not a failure, so
    ConfirmedWriteError is swallowed here. The zero-write contract exists to
    protect DATA tables; a metering table that refused to re-write an unchanged
    counter would just alert forever.

    Not metered itself (see `_upsert`'s meter flag) so the ledger cannot feed
    its own counter.
    """
    if not rows:
        return 0
    try:
        return _upsert("api_usage", API_USAGE_COLS, rows,
                       ["bucket", "period", "source"],
                       ["calls", "provider_remaining", "provider_limit",
                        "provider_used", "updated_at"])
    except ConfirmedWriteError as e:
        print(f"[d1_store] api_usage no-op: {e}")
        return 0


def upsert_stat_observations(rows: list[dict]) -> int:
    now = datetime.now(timezone.utc).isoformat()
    for r in rows:
        r.setdefault("subject_type", "team")
        r.setdefault("source", "cfbd")
        r.setdefault("week", 0)
        r.setdefault("recorded_at", now)
    return _upsert("stat_observations",
                   ["subject_type", "subject_id", "season", "week", "stat_key",
                    "value", "source", "recorded_at"],
                   rows, ["subject_type", "subject_id", "season", "stat_key", "week"],
                   ["value", "source", "recorded_at"])


def upsert_games(rows: list[dict]) -> int:
    return _upsert("games",
                   ["game_id", "season", "week", "home_id", "away_id", "kickoff",
                    "home_score", "away_score", "status", "venue", "neutrality"],
                   rows, ["game_id"],
                   ["home_score", "away_score", "status", "kickoff", "venue", "neutrality"])


def append_odds_snapshots(rows: list[dict]) -> int:
    now = datetime.now(timezone.utc).isoformat()
    for r in rows:
        r.setdefault("poll_ts", now)
    return _upsert("odds_snapshots",
                   ["game_id", "book", "spread_home", "total", "home_ml", "away_ml", "poll_ts"],
                   rows, None, None)


def append_freshness_events(rows: list[dict]) -> int:
    """Append freshness telemetry (container starts, pull attempts + outcomes).

    Exists because the 2026-09-22 staleness incident was undiagnosable after the
    fact: the only evidence was a console.log in the Worker's scheduled() handler,
    and retained Workers Logs are not enabled, so nothing could be queried later.
    Append-only, no conflict target, low volume.
    """
    now = datetime.now(timezone.utc).isoformat()
    for r in rows:
        r.setdefault("ts_utc", now)
    return _upsert("freshness_events",
                   ["ts_utc", "event", "source", "age_hours", "build_tag", "detail"],
                   rows, None, None)


def append_backtest_runs(rows: list[dict]) -> int:
    """Archive one row per ARM per backtest experiment (see d1/schema.sql).

    Gives experiments a durable home so "did this weighting help?" can be answered by
    querying history instead of re-running and hoping the old numbers were remembered.
    Idempotent on (run_id, arm): re-archiving the same run updates rather than duplicates.
    """
    cols = ["run_id", "ts_utc", "arm", "model_version", "weights_json", "season",
            "games", "fbs_matchups", "ats_all", "ats_3star", "ats_5star",
            "totals_all", "totals_3star", "dog_share_pct", "mean_model_margin",
            "mean_book_line", "mae_vs_book", "bias_vs_book", "note"]
    update = [c for c in cols if c not in ("run_id", "arm")]
    return _upsert("backtest_runs", cols, rows, ["run_id", "arm"], update)


def append_served_snapshots(rows: list[dict]) -> int:
    """Append a daily served-state snapshot (see d1/schema.sql).

    One row per endpoint per day: the data's own `as_of`, a row count, and a content
    hash. Exists so a future staleness incident has an answer to "what did users
    actually see" — the 2026-09-22 one could not be answered at all.
    """
    now = datetime.now(timezone.utc).isoformat()
    for r in rows:
        r.setdefault("ts_utc", now)
        r.setdefault("date", now[:10])
    return _upsert("served_snapshots",
                   ["date", "ts_utc", "endpoint", "as_of", "row_count",
                    "content_hash", "build_tag", "detail"],
                   rows, None, None)


def get_app_state(key: str) -> str | None:
    """Read a durable value. Returns None when absent (never raises for a miss)."""
    rows = query("SELECT value FROM app_state WHERE key = ?", [key])
    return rows[0]["value"] if rows else None


def set_app_state(key: str, value: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    return _upsert("app_state", ["key", "value", "updated_at"],
                   [{"key": key, "value": value, "updated_at": now}],
                   ["key"], ["value", "updated_at"])


def upsert_rankings_daily(rows: list[dict]) -> int:
    """Write the daily rankings snapshot — replacing the WHOLE date partition.

    A daily archive must hold that day's SINGLE view of the rankings. The generic
    key-based replace only deletes the keys present in the batch, so a second write
    on the same day left the first write's rows behind: observed 2026-09-22 with 26
    rows for one date, two different teams (ids 344 and 145) both sitting at rank
    25. Deleting the date first makes the partition exactly one row per team.

    (The second write came from an ephemeral once-a-day marker file — see
    d1_write_path.daily_rankings, which now checks D1 itself.)
    """
    cols = ["team_id", "date", "composite", "rank", "model_version", "season", "week"]
    if not rows:
        return 0
    n = 0
    cleared: set = set()
    for part in _chunks(rows, max(1, 100 // len(cols))):
        new_dates = sorted({r.get("date") for r in part if r.get("date")} - cleared)
        if new_dates:
            ph = ",".join("?" * len(new_dates))
            assert_headroom(len(new_dates))
            _, meta_del = query_full(
                f"DELETE FROM rankings_daily WHERE date IN ({ph})", new_dates)
            confirmed_del = confirmed_writes(meta_del)
            commit_writes(confirmed_del)
            n += confirmed_del
            cleared.update(new_dates)
        assert_headroom(len(part))
        ph2 = ",".join("(" + ",".join("?" * len(cols)) + ")" for _ in part)
        _, meta_ins = query_full(
            f"INSERT INTO rankings_daily ({','.join(cols)}) VALUES {ph2}",
            [v for r in part for v in (r.get(c) for c in cols)])
        confirmed = confirmed_writes(meta_ins)
        if confirmed <= 0:
            raise ConfirmedWriteError(
                f"rankings_daily: D1 confirmed 0 rows written for a {len(part)}-row batch "
                f"(meta={meta_ins})")
        commit_writes(confirmed)
        n += confirmed
    return n


def upsert_closing_lines(rows: list[dict]) -> int:
    now = datetime.now(timezone.utc).isoformat()
    for r in rows:
        r.setdefault("captured_at", now)
    return _replace_by("closing_lines", ["game_id", "book"],
                       ["game_id", "book", "spread_home", "total",
                        "home_moneyline", "away_moneyline", "captured_at"], rows)


def insert_model_predictions(rows: list[dict]) -> int:
    now = datetime.now(timezone.utc).isoformat()
    for r in rows:
        r.setdefault("created_at", now)
    return _replace_by("model_predictions", ["game_id", "model_version"],
                       ["game_id", "model_version", "predicted_margin_home",
                        "predicted_total", "win_prob_home", "created_at",
                        # Part 4: weather the model actually saw at write time.
                        "wind_mph", "temp_f", "condition", "indoor", "wind_penalty"],
                       rows)


def insert_weather_snapshots(rows: list[dict]) -> int:
    """weather_snapshots — one row per game per poll (our own weather history).

    Natural key (game_id, poll_ts): a retry inside the same poll replaces rather
    than duplicates, while the next poll's rows are new rows on purpose — the
    series over time is the point. Values come from the SAME parser the model
    consumes (_cfbd_weather via d1_write_path._wx), so this table can never
    disagree with what the model saw.
    """
    return _replace_by("weather_snapshots", ["game_id", "poll_ts"],
                       ["game_id", "season", "week", "kickoff_utc", "poll_ts",
                        "wind_mph", "temp_f", "condition", "indoor"],
                       rows)


def insert_injuries(rows: list[dict]) -> int:
    """Part 2: one row per team per game, written pre-kickoff.

    The unique index ux_injury_snap_team_game lets us INSERT OR REPLACE — a re-serve of
    the same game upserts rather than duplicating, and the FIRST write of the day wins
    the timestamp because a later one is only allowed while the game is still upcoming.
    """
    now = datetime.now(timezone.utc).isoformat()
    for r in rows:
        r.setdefault("created_at", now)
        if not isinstance(r.get("injury_list"), str):
            r["injury_list"] = json.dumps(r.get("injury_list") or [])
    cols = ["game_id", "season", "week", "team", "opponent", "kickoff_ts",
            "injury_list", "injury_adj_applied", "predicted_margin", "predicted_total",
            "model_version", "created_at"]
    n = 0
    step = max(1, 100 // len(cols))
    for part in _chunks(rows, step):
        assert_headroom(len(part))
        ph = ",".join("(" + ",".join("?" * len(cols)) + ")" for _ in part)
        sql = (f"INSERT OR REPLACE INTO injury_snapshots ({','.join(cols)}) "
               f"VALUES {ph}")
        _, meta = query_full(sql, [v for r in part for v in (r.get(c) for c in cols)])
        confirmed = confirmed_writes(meta)
        if confirmed <= 0:
            raise ConfirmedWriteError(
                f"injury_snapshots: D1 confirmed 0 rows for a {len(part)}-row insert")
        commit_writes(confirmed)
        n += confirmed
    return n


def append_raw_payloads(rows: list[dict]) -> int:
    """raw_payloads — the full upstream response, kept verbatim (Part 3).

    The 2026-09-23 lesson: this filing cabinet is why a payload archive can exist at all.
    Cloudflare Containers run on an EPHEMERAL filesystem — anything written to data/ at
    runtime is gone on the next instance recycle, so a file-only "daily snapshot" silently
    accumulates nothing while looking perfectly healthy. The row is the archive; the file
    is a convenience copy for local dev.

    Payloads are gzipped and base64'd into payload_gz. Stored as text rather than a D1
    blob because the HTTP API has no unambiguous blob parameter form; SQLite is typeless,
    so the column accepts it and a reader just b64-decodes then gunzips.

    Replaces on `endpoint`, so writing the same day twice keeps the latest — one row per
    day, matching the order's "one file write per day".
    """
    import base64  # noqa: PLC0415
    import gzip  # noqa: PLC0415

    now = datetime.now(timezone.utc).isoformat()
    out = []
    for r in rows:
        payload = r.get("payload")
        if payload is None:
            continue
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        elif not isinstance(payload, (bytes, bytearray)):
            payload = json.dumps(payload).encode("utf-8")
        out.append({
            "endpoint": r["endpoint"],
            "fetched_at": r.get("fetched_at", now),
            "payload_gz": base64.b64encode(gzip.compress(bytes(payload))).decode("ascii"),
        })
    if not out:
        return 0
    return _replace_by("raw_payloads", ["endpoint"],
                       ["endpoint", "fetched_at", "payload_gz"], out)


def save_slate(season: int, week: int, fingerprint: str, model_version: str,
               payload_json: str) -> int:
    """Store the finished Schedule payload for (season, week) — one row per week.

    Payloads are gzipped and base64'd into payload_gz, matching insert_raw_payloads:
    the D1 HTTP API has no unambiguous blob parameter form, and SQLite is typeless.

    Replaces on (season, week) so readers never see two candidate slates and the table
    cannot grow per poll. The 80KB JSON compresses to ~8KB.
    """
    import base64  # noqa: PLC0415
    import gzip  # noqa: PLC0415

    now = datetime.now(timezone.utc).isoformat()
    blob = base64.b64encode(gzip.compress(payload_json.encode("utf-8"))).decode("ascii")
    return _replace_by("slate_cache", ["season", "week"],
                       ["season", "week", "built_at", "fingerprint",
                        "model_version", "payload_gz"],
                       [{"season": season, "week": week, "built_at": now,
                         "fingerprint": fingerprint, "model_version": model_version,
                         "payload_gz": blob}])


def load_slate(season: int, week: int) -> dict | None:
    """Read the stored slate. Returns {payload, fingerprint, model_version, ts} or None.

    `ts` is epoch seconds so the caller can decide staleness without re-parsing ISO.
    """
    import base64  # noqa: PLC0415
    import gzip  # noqa: PLC0415

    rows = query("SELECT payload_gz, fingerprint, model_version, built_at "
                 "FROM slate_cache WHERE season = ? AND week = ?", [season, week])
    if not rows:
        return None
    r = rows[0]
    blob = r.get("payload_gz")
    if not blob:
        return None
    try:
        text = gzip.decompress(base64.b64decode(blob)).decode("utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"[slate_store] decode failed: {e}")
        return None
    ts = 0.0
    try:
        ts = datetime.fromisoformat(str(r.get("built_at"))).timestamp()
    except Exception:  # noqa: BLE001
        pass
    return {"payload": text, "fingerprint": r.get("fingerprint"),
            "model_version": r.get("model_version"), "ts": ts}


def health() -> dict:
    """Cheap read used by the degraded-mode path (B4)."""
    try:
        rows = query("SELECT COUNT(*) AS n FROM teams")
        return {"ok": True, "teams": rows[0]["n"] if rows else 0}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    print(json.dumps(health(), indent=2))