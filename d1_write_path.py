#!/usr/bin/env python3
"""d1_write_path.py — the LIVE write-path (D1_SCHEMA_SPEC §5).

Appends the app's live data into D1 so the archive grows by itself:
  * hourly refresh  -> odds_snapshots (append-only, every game, every poll)
  * nightly refresh -> rankings_daily (one row per team per day)
  * game finals     -> closing_lines
  * pre-kickoff     -> model_predictions

Two hard rules (from D1_RISK_REGISTER):
  B4: a D1 failure must NEVER break the site. Every entry point is wrapped so
      it returns 0 and logs instead of raising.
  A4: gated by D1_WRITE_ENABLED. Default OFF -> the app behaves exactly as before.
      Rollback = unset the flag.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import d1_store


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


def team_name_to_id() -> dict:
    return {r["name"]: r["team_id"] for r in d1_store.query("SELECT team_id, name FROM teams")}


def game_ids_by_pair(season: int | None = None) -> dict:
    """{(home_name, away_name): game_id} from D1 games joined to team names."""
    sql = ("SELECT g.game_id, h.name AS home, a.name AS away "
           "FROM games g LEFT JOIN teams h ON h.team_id = g.home_id "
           "LEFT JOIN teams a ON a.team_id = g.away_id")
    params = []
    if season:
        sql += " WHERE g.season = ?"
        params.append(season)
    return {(r["home"], r["away"]): r["game_id"]
            for r in d1_store.query(sql, params) if r["home"] and r["away"]}


def _norm_pair(odds_map: dict, pairs: dict, normalizer=None) -> dict:
    """Map (home, away) odds keys onto D1 game_ids, using the app's own normalizer
    when provided (bookmaker names differ from CFBD school names)."""
    out = {}
    for (home, away), v in odds_map.items():
        gid = pairs.get((home, away))
        if gid is None and normalizer:
            gid = pairs.get((normalizer(home), normalizer(away)))
        if gid is None:
            continue
        out[gid] = v
    return out


@_guard
def snapshot_odds(odds_map: dict, normalizer=None) -> int:
    """Append one odds_snapshots row per game for this poll (spec §5.2)."""
    if not odds_map:
        return 0
    pairs = game_ids_by_pair()
    matched = _norm_pair(odds_map, pairs, normalizer)
    ts = _now()
    rows = [{"game_id": gid, "book": v.get("book_title") or v.get("book") or "best",
             "spread_home": v.get("spread"), "total": v.get("total"),
             "home_ml": v.get("home_ml"), "away_ml": v.get("away_ml"), "poll_ts": ts}
            for gid, v in matched.items()]
    return d1_store.append_odds_snapshots(rows)


@_guard
def snapshot_rankings(rankings: list[dict], season: int | None = None,
                      week: int | None = None, model_version: str = "composite") -> int:
    """One rankings_daily row per team (spec §5.3). rankings: {team/name, composite, rank}."""
    if not rankings:
        return 0
    name2id = team_name_to_id()
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = []
    for r in rankings:
        tid = r.get("team_id") or name2id.get(r.get("team") or r.get("name"))
        if tid is None:
            continue
        rows.append({"team_id": tid, "date": date, "composite": r.get("composite"),
                     "rank": r.get("rank") or r.get("composite_rank"),
                     "model_version": model_version, "season": season, "week": week})
    return d1_store.upsert_rankings_daily(rows)


@_guard
def snapshot_closing_lines(games: list[dict]) -> int:
    """closing_lines from final games (spec §5.4). games: {game_id, ..., spread/total/mls}."""
    rows = [{"game_id": g.get("game_id") or g.get("id"), "book": g.get("book", "consensus"),
             "spread_home": g.get("spread_home") or g.get("spread"),
             "total": g.get("total"),
             "home_moneyline": g.get("home_moneyline"),
             "away_moneyline": g.get("away_moneyline")}
            for g in games if (g.get("game_id") or g.get("id"))]
    return d1_store.upsert_closing_lines(rows)


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