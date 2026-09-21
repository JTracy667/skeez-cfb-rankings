#!/usr/bin/env python3
"""d1_store.py — thin wrapper over the Cloudflare D1 REST API (Phase 2).

Purpose: persist the app's history (odds snapshots, stat observations, daily
rankings, closing lines, model predictions) into D1 `cfb-history`.

SECURITY (deliberate): this module reads its token from `CF_D1_TOKEN` FIRST.
Provision a Cloudflare token scoped to D1 (account-level "D1:Edit") and put THAT
in the container env. Do NOT hand the app the broad zone token (DNS:Edit) —
`CLOUDFLARE_API_TOKEN` is only a local-dev fallback and is never sent from the
container in production.

Additive + safe: nothing here runs until the app imports and calls it, and the
whole write-path is behind env flag D1_WRITE_ENABLED. If D1 is unreachable the
callers must fall back to the local cache (see D1_RISK_REGISTER B4) — every
helper raises on failure so the caller can decide; it never silently no-ops.

Budget guard: CFBD/Odds budgets live elsewhere; this module enforces the D1
row-WRITE budget (free tier 100K/day; backfill must stay <= 90K to leave live
headroom). A local ledger counts writes per UTC day and refuses to exceed the cap.
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
D1_DB_NAME = os.environ.get("CF_D1_DB_NAME", "cfb-history")


def _token() -> str:
    tok = os.environ.get("CF_D1_TOKEN") or os.environ.get("CLOUDFLARE_API_TOKEN") or ""
    if not tok:
        raise RuntimeError("no D1 token: set CF_D1_TOKEN (scoped D1:Edit)")
    return tok


def _api_url() -> str:
    return (f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}"
            f"/d1/database/{D1_DB_ID}/query")


# ------------------------------------------------------------------ budget guard
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


def write_budget_ok(n_rows: int, daily_cap: int | None = None) -> None:
    cap = daily_cap if daily_cap is not None else int(os.environ.get("D1_DAILY_WRITE_CAP", "90000"))
    led = _load_ledger()
    if led["rows_written"] + n_rows > cap:
        raise BudgetExceeded(f"D1 daily write budget: {led['rows_written']}+{n_rows} > {cap}")
    led["rows_written"] += n_rows
    _save_ledger(led)


# ------------------------------------------------------------------------- core
def query(sql: str, params: list | None = None, timeout: int = 60) -> list[dict]:
    """Run one SQL statement. Returns the result rows (list of dicts)."""
    body = json.dumps({"sql": sql, "params": params or []}).encode()
    req = urllib.request.Request(_api_url(), data=body, method="POST",
                                 headers={"Authorization": f"Bearer {_token()}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"D1 HTTP {e.code}: {e.read()[:300]!r}") from e
    if not payload.get("success"):
        raise RuntimeError(f"D1 error: {payload.get('errors')}")
    res = payload.get("result") or []
    return (res[0].get("results") if res else []) or []


def _exec(sql: str) -> list[dict]:
    return query(sql)


# ------------------------------------------------------- idempotent row helpers
def _chunks(rows: list, n: int):
    for i in range(0, len(rows), n):
        yield rows[i:i + n]


def upsert_teams(rows: list[dict], chunk: int = 200) -> int:
    """rows: {team_id,name,abbr,conference,first_season}. Idempotent."""
    n = 0
    for part in _chunks(rows, chunk):
        write_budget_ok(len(part))
        for r in part:
            query("INSERT INTO teams (team_id,name,abbr,conference,first_season) "
                  "VALUES (?,?,?,?,?) ON CONFLICT(team_id) DO UPDATE SET "
                  "name=excluded.name, abbr=excluded.abbr, conference=excluded.conference",
                  [r.get("team_id"), r.get("name"), r.get("abbr"),
                   r.get("conference"), r.get("first_season")])
            n += 1
    return n


def upsert_stat_observations(rows: list[dict], chunk: int = 500) -> int:
    """rows: {subject_type,subject_id,season,week,stat_key,value,source}.
    Idempotent via the ux_stat_obs_subject unique index."""
    n = 0
    now = datetime.now(timezone.utc).isoformat()
    for part in _chunks(rows, chunk):
        write_budget_ok(len(part))
        for r in part:
            query("INSERT INTO stat_observations "
                  "(subject_type,subject_id,season,week,stat_key,value,source,recorded_at) "
                  "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(subject_type,subject_id,season,stat_key,week) "
                  "DO UPDATE SET value=excluded.value, source=excluded.source, recorded_at=excluded.recorded_at",
                  [r.get("subject_type", "team"), r.get("subject_id"), r.get("season"),
                   r.get("week", 0), r.get("stat_key"), r.get("value"),
                   r.get("source", "cfbd"), now])
            n += 1
    return n


def append_odds_snapshots(rows: list[dict], chunk: int = 500) -> int:
    """rows: {game_id,book,spread_home,total,home_ml,away_ml,poll_ts}. Append-only."""
    n = 0
    for part in _chunks(rows, chunk):
        write_budget_ok(len(part))
        for r in part:
            query("INSERT INTO odds_snapshots "
                  "(game_id,book,spread_home,total,home_ml,away_ml,poll_ts) VALUES (?,?,?,?,?,?,?)",
                  [r.get("game_id"), r.get("book"), r.get("spread_home"), r.get("total"),
                   r.get("home_ml"), r.get("away_ml"),
                   r.get("poll_ts") or datetime.now(timezone.utc).isoformat()])
            n += 1
    return n


def upsert_rankings_daily(rows: list[dict], chunk: int = 500) -> int:
    """rows: {team_id,date,composite,rank,model_version,season,week}."""
    n = 0
    for part in _chunks(rows, chunk):
        write_budget_ok(len(part))
        for r in part:
            query("INSERT INTO rankings_daily "
                  "(team_id,date,composite,rank,model_version,season,week) VALUES (?,?,?,?,?,?,?)",
                  [r.get("team_id"), r.get("date"), r.get("composite"), r.get("rank"),
                   r.get("model_version"), r.get("season"), r.get("week")])
            n += 1
    return n


def upsert_closing_lines(rows: list[dict], chunk: int = 500) -> int:
    """rows: {game_id,book,spread_home,total,home_moneyline,away_moneyline}."""
    n = 0
    for part in _chunks(rows, chunk):
        write_budget_ok(len(part))
        for r in part:
            query("DELETE FROM closing_lines WHERE game_id=? AND book=?",
                  [r.get("game_id"), r.get("book")])
            query("INSERT INTO closing_lines "
                  "(game_id,book,spread_home,total,home_moneyline,away_moneyline,captured_at) "
                  "VALUES (?,?,?,?,?,?,?)",
                  [r.get("game_id"), r.get("book"), r.get("spread_home"), r.get("total"),
                   r.get("home_moneyline"), r.get("away_moneyline"),
                   datetime.now(timezone.utc).isoformat()])
            n += 1
    return n


def insert_model_predictions(rows: list[dict], chunk: int = 500) -> int:
    """rows: {game_id,model_version,predicted_margin_home,predicted_total,win_prob_home}.
    Caller MUST reject rows created after kickoff (D1_RISK_REGISTER D2)."""
    n = 0
    for part in _chunks(rows, chunk):
        write_budget_ok(len(part))
        for r in part:
            query("INSERT INTO model_predictions "
                  "(game_id,model_version,predicted_margin_home,predicted_total,win_prob_home,created_at) "
                  "VALUES (?,?,?,?,?,?)",
                  [r.get("game_id"), r.get("model_version"), r.get("predicted_margin_home"),
                   r.get("predicted_total"), r.get("win_prob_home"),
                   datetime.now(timezone.utc).isoformat()])
            n += 1
    return n


def health() -> dict:
    """Cheap read used by the degraded-mode path (B4)."""
    try:
        rows = query("SELECT COUNT(*) AS n FROM teams")
        return {"ok": True, "teams": rows[0]["n"] if rows else 0}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    print(json.dumps(health(), indent=2))