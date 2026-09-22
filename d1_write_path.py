#!/usr/bin/env python3
"""d1_write_path.py — the LIVE write-path (D1_SCHEMA_SPEC §5).

Appends the app's live data into D1 so the archive grows by itself:
  * hourly refresh  -> odds_snapshots (append-only, every game, every poll)
  * daily           -> rankings_daily (one row per team per day)
  * finals          -> closing_lines
  * pre-kickoff     -> model_predictions

Two hard rules (from D1_RISK_REGISTER):
  B4: a D1 failure must NEVER break the site. Every entry point is wrapped so
      it returns 0 and logs instead of raising.
  A4: gated by D1_WRITE_ENABLED. Default OFF -> the app behaves exactly as before.
      Rollback = unset the flag.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import d1_store

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_RANK_DATE_FILE = os.path.join(BASE_DIR, "data", "d1_rankings_last.json")


def enabled() -> bool:
    return os.environ.get("D1_WRITE_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def _guard(fn):
    """Never let an archive failure reach the site."""
    def wrapper(*a, **k):
        if not enabled():
            return 0
        try:
            return fn(*a, **k)
        except Exception as e:  # noqa: BLE001
            print(f"[d1_write_path] {fn.__name__} failed (site unaffected): {e}")
            return 0
    wrapper.__name__ = fn.__name__
    return wrapper


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get(r, k):
    """Read a field from a dict OR a pydantic model."""
    return r.get(k) if isinstance(r, dict) else getattr(r, k, None)


def team_name_to_id() -> dict:
    return {r["name"]: r["team_id"] for r in d1_store.query("SELECT team_id, name FROM teams")}


def game_ids_by_pair(season: int | None = None, normalizer=None) -> dict:
    """{(home_name, away_name): game_id} from D1 games joined to team names.
    When a normalizer is supplied both raw and normalized keys are registered, so
    bookmaker-style names match the CFBD school names stored in D1."""
    sql = ("SELECT g.game_id, h.name AS home, a.name AS away "
           "FROM games g LEFT JOIN teams h ON h.team_id = g.home_id "
           "LEFT JOIN teams a ON a.team_id = g.away_id")
    params = []
    if season:
        sql += " WHERE g.season = ?"
        params.append(season)
    out = {}
    for r in d1_store.query(sql, params):
        home, away = r["home"], r["away"]
        if not home or not away:
            continue
        out[(home, away)] = r["game_id"]
        if normalizer:
            out.setdefault((normalizer(home), normalizer(away)), r["game_id"])
    return out


def _resolve(pairs: dict, normalizer, home, away):
    gid = pairs.get((home, away))
    if gid is None and normalizer:
        gid = pairs.get((normalizer(home), normalizer(away)))
    return gid


@_guard
def snapshot_odds(odds_map: dict, normalizer=None) -> int:
    """Append one odds_snapshots row per game for this poll (spec §5.2)."""
    if not odds_map:
        return 0
    pairs = game_ids_by_pair(normalizer=normalizer)
    ts = _now()
    rows = []
    for (home, away), v in odds_map.items():
        gid = _resolve(pairs, normalizer, home, away)
        if gid is None:
            continue
        rows.append({"game_id": gid, "book": v.get("book_title") or v.get("book") or "best",
                     "spread_home": v.get("spread"), "total": v.get("total"),
                     "home_ml": v.get("home_ml"), "away_ml": v.get("away_ml"), "poll_ts": ts})
    return d1_store.append_odds_snapshots(rows)


@_guard
def snapshot_closing(closing_map: dict, normalizer=None, book: str = "consensus") -> int:
    """closing_lines from the app's closing-line map {(home,away): {spread,total}}.
    Idempotent: upsert keyed on (game_id, book)."""
    if not closing_map:
        return 0
    pairs = game_ids_by_pair(normalizer=normalizer)
    rows = []
    for (home, away), v in closing_map.items():
        gid = _resolve(pairs, normalizer, home, away)
        if gid is None:
            continue
        rows.append({"game_id": gid, "book": book,
                     "spread_home": v.get("spread"), "total": v.get("total"),
                     "home_moneyline": v.get("home_moneyline"),
                     "away_moneyline": v.get("away_moneyline")})
    return d1_store.upsert_closing_lines(rows)


@_guard
def snapshot_rankings(teams, season: int | None = None, week: int | None = None,
                      model_version: str = "composite") -> int:
    """One rankings_daily row per team (spec §5.3). teams: Team models or dicts."""
    if not teams:
        return 0
    name2id = team_name_to_id()
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = []
    for t in teams:
        tid = (_get(t, "team_id") or name2id.get(_get(t, "name"))
               or name2id.get(_get(t, "location")))
        if tid is None:
            continue
        rows.append({"team_id": tid, "date": date, "composite": _get(t, "composite"),
                     "rank": _get(t, "rank") or _get(t, "composite_rank"),
                     "model_version": model_version, "season": season, "week": week})
    return d1_store.upsert_rankings_daily(rows)


def daily_rankings(fetch_teams, season: int | None = None, week: int | None = None) -> int:
    """Write rankings_daily at most ONCE per UTC day. `fetch_teams` is only called on
    the day it actually writes, so other hourly ticks cost nothing. The date is stamped
    only when rows landed, so a failure simply retries on the next tick."""
    if not enabled():
        return 0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        with open(_RANK_DATE_FILE, encoding="utf-8") as f:
            if json.load(f).get("date") == today:
                return 0
    except Exception:  # noqa: BLE001
        pass
    try:
        n = snapshot_rankings(fetch_teams(), season, week)
        if n:
            os.makedirs(os.path.dirname(_RANK_DATE_FILE), exist_ok=True)
            with open(_RANK_DATE_FILE, "w", encoding="utf-8") as f:
                json.dump({"date": today, "rows": n}, f)
        return n
    except Exception as e:  # noqa: BLE001
        print(f"[d1_write_path] daily_rankings failed (site unaffected): {e}")
        return 0


@_guard
def snapshot_predictions(games: list[dict], model_version: str = "composite") -> int:
    """model_predictions — MUST be called before kickoff (risk register D2)."""
    rows = [{"game_id": g.get("game_id") or g.get("id"), "model_version": model_version,
             "predicted_margin_home": g.get("predicted_margin_home"),
             "predicted_total": g.get("predicted_total"),
             "win_prob_home": g.get("win_prob_home")}
            for g in games if (g.get("game_id") or g.get("id"))]
    return d1_store.insert_model_predictions(rows)


@_guard
def health() -> int:
    return 1 if d1_store.health().get("ok") else 0