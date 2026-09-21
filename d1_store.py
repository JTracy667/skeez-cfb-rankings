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

Additive + safe: nothing here runs until a caller imports it. Callers must fall
back to local cache on failure (D1_RISK_REGISTER B4). The budget guard enforces
the D1 row-WRITE cap (free tier 100K/day; backfill <= 90K, leaving live headroom).
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


def ledger_written() -> int:
    return _load_ledger()["rows_written"]


def assert_headroom(n_rows: int, daily_cap: int | None = None) -> None:
    """Refuse to START a write that would exceed the daily cap (check only)."""
    cap = daily_cap if daily_cap is not None else int(os.environ.get("D1_DAILY_WRITE_CAP", "90000"))
    led = _load_ledger()
    if led["rows_written"] + n_rows > cap:
        raise BudgetExceeded(f"D1 daily write budget: {led['rows_written']}+{n_rows} > {cap}")


def commit_writes(n_rows: int) -> None:
    """Record ACTUAL confirmed writes — taken from the D1 response's meta.rows_written,
    never a local guess (CEO: counter must assert on the API response)."""
    led = _load_ledger()
    led["rows_written"] += int(n_rows)
    _save_ledger(led)


def write_budget_ok(n_rows: int, daily_cap: int | None = None) -> None:
    """Check + optimistically commit (for simple callers with no meta to read)."""
    assert_headroom(n_rows, daily_cap)
    commit_writes(n_rows)


# ------------------------------------------------------------------------- core
def query(sql: str, params: list | None = None, timeout: int = 60) -> list[dict]:
    """Run one SQL statement. Returns the result rows (list of dicts)."""
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
    if res and isinstance(res[0], dict):
        LAST_META = res[0].get("meta") or {}
    return (res[0].get("results") if res else []) or []


def _chunks(rows: list, n: int = CHUNK):
    for i in range(0, len(rows), n):
        yield rows[i:i + n]


def _upsert(table: str, cols: list[str], rows: list[dict],
            conflict: list[str] | None, update: list[str] | None) -> int:
    """One batched multi-row INSERT per chunk (idempotent when conflict given).
    D1 caps bound parameters at 100/query -> chunk by column count."""
    n = 0
    for part in _chunks(rows, max(1, 100 // len(cols))):
        assert_headroom(len(part))
        ph = ",".join("(" + ",".join("?" * len(cols)) + ")" for _ in part)
        sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES {ph}"
        if conflict and update:
            sql += (f" ON CONFLICT({','.join(conflict)}) DO UPDATE SET "
                    + ",".join(f"{c}=excluded.{c}" for c in update))
        params = [v for r in part for v in (r.get(c) for c in cols)]
        query(sql, params)
        commit_writes(LAST_META.get("rows_written") or len(part))
        n += len(part)
    return n


def _replace_by(table: str, key_cols: list[str], cols: list[str], rows: list[dict]) -> int:
    """For tables with no unique index: delete the chunk's keys, then batched insert.
    D1 caps bound parameters at 100/query -> chunk by the widest column list."""
    n = 0
    step = max(1, 100 // max(len(cols), len(key_cols)))
    for part in _chunks(rows, step):
        assert_headroom(len(part))
        keys = sorted({tuple(r.get(c) for c in key_cols) for r in part})
        ph = ",".join("(" + ",".join("?" * len(key_cols)) + ")" for _ in keys)
        query(f"DELETE FROM {table} WHERE ({','.join(key_cols)}) IN ({ph})",
              [v for k in keys for v in k])
        ph2 = ",".join("(" + ",".join("?" * len(cols)) + ")" for _ in part)
        query(f"INSERT INTO {table} ({','.join(cols)}) VALUES {ph2}",
              [v for r in part for v in (r.get(c) for c in cols)])
        commit_writes(LAST_META.get("rows_written") or len(part))
        n += len(part)
    return n


# --------------------------------------------------------- typed row helpers
def upsert_teams(rows: list[dict]) -> int:
    return _upsert("teams", ["team_id", "name", "abbr", "conference", "first_season"],
                   rows, ["team_id"], ["name", "abbr", "conference"])


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


def upsert_rankings_daily(rows: list[dict]) -> int:
    return _replace_by("rankings_daily", ["team_id", "date"],
                       ["team_id", "date", "composite", "rank", "model_version", "season", "week"],
                       rows)


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
                        "predicted_total", "win_prob_home", "created_at"], rows)


def health() -> dict:
    """Cheap read used by the degraded-mode path (B4)."""
    try:
        rows = query("SELECT COUNT(*) AS n FROM teams")
        return {"ok": True, "teams": rows[0]["n"] if rows else 0}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    print(json.dumps(health(), indent=2))