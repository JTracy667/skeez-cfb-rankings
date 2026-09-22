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


def daily_rankings(fetch_teams, season: int | None = None, week: int | None = None,
                   model_version: str = "composite") -> int:
    """Write rankings_daily at most ONCE per UTC day. `fetch_teams` is only called on
    the day it actually writes, so other hourly ticks cost nothing. The date is stamped
    only when rows landed, so a failure simply retries on the next tick."""
    if not enabled():
        return 0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Fast path: the local marker file (works on the desktop and within a single
    # container instance's lifetime). Version-aware for the same reason as the D1
    # guard below.
    try:
        with open(_RANK_DATE_FILE, encoding="utf-8") as f:
            marker = json.load(f)
        if marker.get("date") == today and marker.get("model_version") == model_version:
            return 0
    except Exception:  # noqa: BLE001
        pass
    # Durable path — the container filesystem is EPHEMERAL, so that marker is
    # wiped on every recycle and this function then wrote the day a SECOND time.
    # Because the writer replaces only its own batch's keys, the two writes
    # unioned: 2026-09-22 ended with 26 rows for one date and two different teams
    # (ids 344 and 145) both at rank 25. D1 is the authority on "already written".
    #
    # VERSION-AWARE (Sep 22 2026, QA finding): the guard used to key on the DATE
    # alone, so once a partition existed under the legacy literal "composite" it was
    # never rewritten — meaning a mid-day config change could NEVER appear in the
    # archive. That defeats risk-register D3, whose entire purpose is that a weight
    # change produces a VISIBLE version break. Now the guard skips only when today's
    # rows already carry the CURRENT model_version; a different version forces a
    # rewrite (safe — the writer replaces the whole date partition).
    try:
        rows = d1_store.query(
            "SELECT model_version, COUNT(*) AS n FROM rankings_daily WHERE date = ? "
            "GROUP BY model_version", [today])
        for r in (rows or []):
            if int(r.get("n") or 0) > 0 and (r.get("model_version") or "") == model_version:
                print(f"[d1_write_path] rankings_daily already written for {today} "
                      f"under {model_version} (D1 guard) — skip")
                return 0
        if rows:
            found = [r.get("model_version") for r in rows]
            print(f"[d1_write_path] rankings_daily for {today} carries {found} != "
                  f"{model_version} — REWRITING so the version break is visible")
    except Exception as e:  # noqa: BLE001
        print(f"[d1_write_path] rankings_daily day-guard read failed: {e}")
    try:
        n = snapshot_rankings(fetch_teams(), season, week, model_version)
        if n:
            os.makedirs(os.path.dirname(_RANK_DATE_FILE), exist_ok=True)
            with open(_RANK_DATE_FILE, "w", encoding="utf-8") as f:
                json.dump({"date": today, "rows": n, "model_version": model_version}, f)
        return n
    except Exception as e:  # noqa: BLE001
        print(f"[d1_write_path] daily_rankings failed (site unaffected): {e}")
        return 0


@_guard
def snapshot_predictions(games: list[dict], model_version: str = "composite") -> int:
    """model_predictions — MUST be written before kickoff (risk register D2).

    D2 guard: a row whose kickoff has ALREADY passed is REJECTED here, at the
    write boundary, rather than trusted to each caller. A post-hoc prediction is
    hindsight and would poison every backtest built on this table while looking
    perfectly healthy — the worst kind of silent corruption.

    `games` carry: game_id, kickoff/date (ISO UTC), predicted_margin_home,
    predicted_total, win_prob_home.
    """
    now = datetime.now(timezone.utc)
    rows = []
    rejected = 0
    for g in games:
        gid = g.get("game_id") or g.get("id")
        if not gid:
            continue
        kick = g.get("kickoff") or g.get("date")
        if kick:
            try:
                if datetime.fromisoformat(str(kick).replace("Z", "+00:00")) <= now:
                    rejected += 1
                    continue
            except Exception:  # noqa: BLE001 — unparsable kickoff is not a licence to write
                rejected += 1
                continue
        else:
            # No kickoff at all: we cannot prove this is pre-game, so refuse.
            rejected += 1
            continue
        margin = g.get("predicted_margin_home")
        total = g.get("predicted_total")
        wp = g.get("win_prob_home")
        if margin is None and total is None and wp is None:
            continue
        rows.append({"game_id": gid, "model_version": model_version,
                     "predicted_margin_home": margin,
                     "predicted_total": total,
                     "win_prob_home": wp})
    if rejected:
        print(f"[d1_write_path] predictions: {rejected} row(s) REFUSED (kickoff already passed "
              f"or unknown — D2 no-hindsight guard)")
    if not rows:
        return 0
    return d1_store.insert_model_predictions(rows)


@_guard
def health() -> int:
    return 1 if d1_store.health().get("ok") else 0


@_guard
def record_freshness_event(event: str, source: str, age_hours: float | None = None,
                           detail: str | None = None) -> int:
    """Append one freshness telemetry row (table: freshness_events).

    Wired at the points that matter for freshness: container start, and every
    analytics pull attempt with its outcome. Guarded like everything else here —
    telemetry must never be able to affect the site.

    `event` : container_start | pull_success | pull_skip | pull_failed
    `source`: scheduler | cron | guard | manual  (what triggered it)
    """
    return d1_store.append_freshness_events([{
        "event": event,
        "source": source,
        "age_hours": age_hours,
        "build_tag": os.environ.get("BUILD_TAG", "dev"),
        "detail": (detail or "")[:500],
    }])


@_guard
def snapshot_served_state(builder, force: bool = False) -> int:
    """Record what the site is currently serving, per endpoint.

    Exists so a future staleness incident can answer "what did users actually see?".
    The 2026-09-22 incident could not be answered at all, because nothing retained
    the served state.

    `builder` is a zero-arg callable returning a list of dicts:
        {endpoint, as_of, row_count, content_hash, detail}
    It IS invoked on every call — it is expected to be cheap (read in-memory
    caches only, never trigger an upstream fetch), because the content hash is what
    decides whether anything is written. The WRITE is what gets skipped.

    Change-driven rather than one-row-per-day: an endpoint is re-recorded only when
    its content hash differs from the last row already stored for today. That yields
    a timeline of served states — enough to pin down exactly when and for how long a
    given value was live — instead of a single opaque daily sample. Bounded by real
    changes, so volume stays small.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    last: dict[str, str] = {}
    try:
        for r in d1_store.query(
                "SELECT endpoint, content_hash FROM served_snapshots "
                "WHERE date = ? ORDER BY id", [today]):
            if r.get("endpoint"):
                last[r["endpoint"]] = r.get("content_hash")   # ordered -> ends on newest
    except Exception:
        pass   # no readable history: fall through and record everything

    rows = []
    for it in (builder() or []):
        ep = it.get("endpoint")
        if not ep:
            continue
        if not force and last.get(ep) == it.get("content_hash"):
            continue                      # unchanged since the last snapshot today
        rows.append({
            "endpoint": ep,
            "as_of": it.get("as_of"),
            "row_count": it.get("row_count"),
            "content_hash": it.get("content_hash"),
            "build_tag": os.environ.get("BUILD_TAG", "dev"),
            "detail": (it.get("detail") or "")[:500],
        })
    if not rows:
        return 0
    return d1_store.append_served_snapshots(rows)