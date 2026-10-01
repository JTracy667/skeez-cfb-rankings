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

import hashlib
import json
from runtime_paths import data_dir as _rt_data_dir
import os
import time
from datetime import datetime, timezone

import d1_store

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_RANK_DATE_FILE = os.path.join(str(_rt_data_dir()), "d1_rankings_last.json")


def write_enabled() -> bool:
    return os.environ.get("D1_WRITE_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def read_enabled() -> bool:
    """Reads default ON and are NOT gated by the write flag (F6).

    WHY THE SPLIT: `D1_WRITE_ENABLED` gated READS too, so with it unset every read
    silently returned [] / None and callers fell back to the ephemeral disk with no
    warning -- and the only way to test that serving reads D1 was to enable WRITES to
    PRODUCTION D1. Reads are the safe direction; they must not require that.
    """
    return os.environ.get("D1_READ_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")


def enabled() -> bool:
    """Backwards-compatible alias for the WRITE gate."""
    return write_enabled()


def _guard(fn):
    """Never let an archive failure reach the site -- and never hide it either (F5).

    Returning 0 and printing to a log the container throws away made a DEAD archive look
    exactly like a healthy one: nothing downstream could tell "wrote 0 rows" apart from
    "never wrote anything". A failed write was indistinguishable from a quiet one, so the
    site could serve stale data with every probe still green.

    The failure is now recorded to D1 `freshness_events` (durable history, already read by
    ops) and surfaced in `/api/health` via `archive_failure_state()`.
    """
    def wrapper(*a, **k):
        if not enabled():
            return 0
        try:
            return fn(*a, **k)
        except Exception as e:  # noqa: BLE001
            print(f"[d1_write_path] {fn.__name__} failed (site unaffected): {e}")
            _record_archive_failure(fn.__name__, e)
            return 0
    wrapper.__name__ = fn.__name__
    return wrapper


# F5: in-process view of archive failures since this container booted. The D1 rows are the
# durable history; this is what /api/health can answer without a query.
_ARCHIVE_FAILURES = {"count": 0, "last_fn": None, "last_error": None, "last_ts": None}
_RECORDING_FAILURE = False   # re-entry guard: the recorder must never recurse (see below)


def archive_failure_state() -> dict:
    """Archive failures since this container booted (F5). Non-zero means writes are
    silently NOT landing -- the state that used to be invisible."""
    return dict(_ARCHIVE_FAILURES)


def _record_archive_failure(fn_name: str, exc: Exception) -> None:
    """Best-effort, and it MUST NOT raise -- and MUST NOT RE-ENTER.

    `record_freshness_event` is itself `@_guard`ed, so when the D1 write path is down its own
    failure calls back into THIS function: guard -> recorder -> guarded recorder -> recorder ->
    ... That recursed until `RecursionError`, which (a) inflated the counter by thousands for
    ONE real failure, (b) overwrote the ORIGINAL error with the recorder's own, so the actual
    cause was never visible, and (c) kept the container's CPU busy enough that it never idled --
    so it never recycled and could not even pick up corrected credentials.

    Observed in production 2026-10-01: archive.count 3450+, last_fn `record_freshness_event`,
    and ZERO `archive_failure` rows in D1 -- because the write that would have recorded the
    failure was the write that was failing.
    """
    global _RECORDING_FAILURE
    if _RECORDING_FAILURE:
        # Already reporting one failure: do not recurse, do not inflate the count.
        print(f"[d1_write_path] (suppressed) {fn_name} failed while reporting a failure: {exc}")
        return
    _ARCHIVE_FAILURES["count"] += 1
    _ARCHIVE_FAILURES["last_fn"] = fn_name
    _ARCHIVE_FAILURES["last_error"] = f"{type(exc).__name__}: {exc}"[:300]
    _ARCHIVE_FAILURES["last_ts"] = _now()
    _RECORDING_FAILURE = True
    try:
        record_freshness_event(event="archive_failure", source=fn_name,
                               detail=_ARCHIVE_FAILURES["last_error"])
    except Exception as e:  # noqa: BLE001
        # If we cannot even record the failure, say so loudly on stdout -- but never raise.
        print(f"[d1_write_path] could not record archive_failure for {fn_name}: {e}")
    finally:
        _RECORDING_FAILURE = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get(r, k):
    """Read a field from a dict OR a pydantic model."""
    return r.get(k) if isinstance(r, dict) else getattr(r, k, None)


def team_name_to_id() -> dict:
    return {r["name"]: r["team_id"] for r in d1_store.query("SELECT team_id, name FROM teams")}


# Task 3 (QA Q3/Q7): `teams` identity is read on EVERY serving request but changes only when
# a pull writes it (locally or in another container), so a bounded TTL is the documented
# trade: at most TEAM_IDENTITY_TTL seconds of lag on the IDENTITY list -- never on the
# numerics, which stay exact via the publication record. A local write invalidates it
# immediately.
TEAM_IDENTITY_TTL = 300
_TEAM_IDENTITY_CACHE: dict = {"at": 0.0, "rows": None}


def invalidate_team_identity_cache() -> None:
    """Called after a successful local `teams` write so identity is never stale locally."""
    _TEAM_IDENTITY_CACHE["at"] = 0.0
    _TEAM_IDENTITY_CACHE["rows"] = None


def team_identity_rows(ttl: int | None = None) -> list[dict]:
    """Every team D1 `teams` knows: {name, conference}. READ-ONLY, bounded-TTL cached.

    Used to make the served universe cover D1's identity list (F3) -- a team D1 knows but the
    site cannot render is drift the parity test is supposed to catch, so the site must be able
    to render all of them.
    """
    ttl = TEAM_IDENTITY_TTL if ttl is None else ttl
    now = time.monotonic()
    cached = _TEAM_IDENTITY_CACHE["rows"]
    if cached is not None and (now - _TEAM_IDENTITY_CACHE["at"]) < ttl:
        return cached
    rows = d1_store.query("SELECT name, conference FROM teams") or []
    _TEAM_IDENTITY_CACHE["rows"] = rows
    _TEAM_IDENTITY_CACHE["at"] = now
    return rows


@_guard
def snapshot_team_identity(rows: list[dict]) -> int:
    """The LIVE WRITER for `teams` (F3).

    WHY: `teams` was READ by the live path but written ONLY by scripts/backfill_d1.py, so a new
    team, a rename or a conference move required a manual backfill and D1's identity list
    silently drifted from the served universe (measured 2026-09-28: D1 684 vs served 685, with
    Anna Maria College and Defiance College unrenderable). Called from the same place the
    analytics archive happens, so identity tracks every live pull instead of a manual run.

    Rows must carry CANONICAL names (cfbd_shared.teams_by_name() -> `school`). Writing an alias
    here would rename a team to a name the rest of the system does not recognise, and
    cfbd_shared.team_aliases() documents that canonical names must always win.
    """
    if not rows:
        return 0
    n = d1_store.upsert_teams(rows)
    if n:
        invalidate_team_identity_cache()
    return n


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


# ── Analytics archive (stat_observations) ─────────────────────────────────────
# The served dataset is data/cfbd_analytics.json — COMMITTED to git and COPY'd into every
# image, then overwritten in place by a live pull. So a deploy silently reverted the
# analytics dataset (the 41 advanced metrics included) to whatever was committed at build
# time, and nothing but the running container's disk ever held it.
#
# stat_observations is the archive the backtest harness already reads, and its unique index
# (subject, season, week, key) makes the write idempotent AND gives backtests the
# point-in-time per-week rows they need. A single JSON blob per pull would hold the same
# numbers but cannot be queried leak-free by week.

# Identity/derived keys that are numeric but are not season metrics.
_ARCHIVE_SKIP_KEYS = frozenset({"team_id", "id", "rank"})


def team_analytics_rows(teams, keys=None, season: int = 0, week: int | None = None,
                        name2id: dict | None = None,
                        source: str = "cfbd") -> list[dict]:
    """Build the stat_observations rows for one analytics pull.

    Pure — the only D1 access is the optional name map — so it is unit-testable without a
    database.

    `keys=None` archives EVERY numeric metric on the record. That is the default on
    purpose: a fixed key list has silently dropped fields twice in this codebase (a
    hand-maintained merge whitelist, and the 41 advanced metrics the parser never kept), so
    the archive must not be able to fall behind the record. Absent and non-numeric values
    are SKIPPED, never written as 0.0 — a missing metric has to stay missing or a backtest
    reads a real zero. `keys` is still accepted for targeted reads/tests.
    """
    if not teams:
        return []
    if name2id is None:
        name2id = team_name_to_id()
    rows: list[dict] = []
    for t in teams:
        tid = (_get(t, "team_id") or name2id.get(_get(t, "name"))
               or name2id.get(_get(t, "location")))
        if tid is None:
            continue
        if keys is None:
            items = t.items() if isinstance(t, dict) else []
            ks = [k for k, v in items
                  if k not in _ARCHIVE_SKIP_KEYS and not isinstance(v, bool)
                  and isinstance(v, (int, float))]
        else:
            ks = keys
        for k in ks:
            v = _get(t, k)
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            rows.append({"subject_type": "team", "subject_id": tid,
                         "season": int(season), "week": int(week or 0),
                         "stat_key": k, "value": float(v), "source": source})
    return rows


# --- Task 4 + 9: only a COMPLETE pull is ever served (QA rulings Q2/Q6) -------------------
# The archive is append-only, so `recorded_at = MAX(recorded_at)` let ANY later writer -- an
# FCS poll, a massey backfill -- become the ENTIRE selected numeric payload for a week: every
# other metric disappeared and the composite silently imputed 50. Selection is now driven by a
# durable publication record written LAST, after every chunk has confirmed, holding the pull's
# stamp, its counts, and the matching string identity. Numerics and identity therefore switch
# together in ONE app_state upsert, and a partial pull is never promoted because it is
# invisible to the selector until the record points at it.
PUBLICATION_KEY_PREFIX = "analytics_publication"

# "the caller did not supply a record" vs "the caller found none". Without this, a
# caller that legitimately read None would trigger a second marker read (Task 3).
_UNSET = object()


def publication_key(season: int) -> str:
    return f"{PUBLICATION_KEY_PREFIX}:{int(season)}"


def analytics_publication(season: int) -> dict | None:
    """The published complete pull for a season. None when absent/unreadable/malformed."""
    if not read_enabled():
        return None
    try:
        blob = d1_store.get_app_state(publication_key(season))
    except Exception as e:  # noqa: BLE001
        print(f"[d1_write_path] publication read failed for {season}: {e}")
        return None
    if not blob:
        return None
    try:
        doc = json.loads(blob)
    except Exception:  # noqa: BLE001
        print(f"[d1_write_path] publication record for {season} is malformed; ignoring it")
        return None
    return doc if isinstance(doc, dict) else None


# QA remediation §2 -- publication is an AUTHORITATIVE act.
#
# The marker names ONE pull as the season's complete snapshot, so it must only be written by
# the full-pull coordinator, and only when that pull's own manifest is complete AND the
# read-back matches it exactly. Before this, ANY call to snapshot_team_analytics could
# publish: a delegated probe that invoked the weekly pull with a one-team payload named
# itself the season's complete snapshot, and every reader then served that stub.
#
# The floor is deliberately about the SHAPE of a season-wide FBS pull (682 teams, ~115 stat
# keys as measured on production data), not a threshold anyone should tune per-week.
MIN_PUBLISH_TEAMS = 600
MIN_PUBLISH_KEYS = 40


def _pull_manifest(rows: list[dict]) -> dict:
    """What this pull intends to write: rows, teams, and the exact set of stat keys."""
    return {"rows": len(rows),
            "teams": len({r.get("subject_id") for r in rows}),
            "keys": sorted({str(r.get("stat_key")) for r in rows})}


def _manifest_reasons(man: dict) -> list[str]:
    why = []
    if man["teams"] < MIN_PUBLISH_TEAMS:
        why.append(f"only {man['teams']} teams (< {MIN_PUBLISH_TEAMS})")
    if len(man["keys"]) < MIN_PUBLISH_KEYS:
        why.append(f"only {len(man['keys'])} stat keys (< {MIN_PUBLISH_KEYS})")
    return why


def _value_digest(pairs) -> str:
    """A stable fingerprint of a (subject_id, stat_key, value) set.

    Both sides canonicalise the value the same way, so an int on one path and a float on the
    other cannot produce a false mismatch. Order-independent (sorted) because the read-back and
    the serving select do not share a row order.
    """
    parts = []
    for sid, key, val in pairs:
        try:
            v = f"{float(val):.6f}"
        except (TypeError, ValueError):
            v = str(val)
        parts.append(f"{int(sid)}|{key}|{v}")
    blob = "\n".join(sorted(parts))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def _read_back_rows(season: int, week: int, stamp: str, expected: int,
                    page: int = 1000) -> list[dict]:
    """Every row actually readable at the stamp: (subject_id, stat_key, value).

    QA counterexample 4: per-key counts and SUMS are not a manifest. Two teams offset by
    +5 and -5 leave every per-key count and sum unchanged, so the wrong values were
    published and then served. The only check that catches that is the row set itself.
    Paged because a season pull is ~35k rows.
    """
    out: list[dict] = []
    offset = 0
    while True:
        rows = d1_store.query(
            "SELECT subject_id, stat_key, value FROM stat_observations "
            "WHERE season = ? AND week = ? AND subject_type = 'team' AND recorded_at = ? "
            "ORDER BY subject_id, stat_key LIMIT ? OFFSET ?",
            [int(season), int(week), str(stamp), int(page), int(offset)]) or []
        out.extend(rows)
        if len(rows) < page:
            break
        offset += page
        if offset > int(expected) + 2 * page:   # runaway guard: never loop forever
            break
    return out


def publish_analytics_pull(season: int, week: int, stamp: str, n_rows: int, n_teams: int,
                           identity: dict | None = None, keys: list[str] | None = None,
                           digest: str | None = None) -> int:
    """Publish a COMPLETE pull: ONE upsert carrying the marker, the identity blob, the manifest
    (row/team counts and the exact stat-key set) and a digest of the verified VALUES, so a
    reader can audit what the marker claims without trusting the writer."""
    doc = {"season": int(season), "week": int(week), "stamp": str(stamp),
           "n_rows": int(n_rows), "n_teams": int(n_teams),
           "keys": sorted(str(k) for k in (keys or [])),
           "n_keys": len(keys or []),
           "digest": str(digest or ""),
           "identity": identity or {}, "published_at": _now()}
    return d1_store.set_app_state(publication_key(season), json.dumps(doc))


def _selection_counts(season: int, week: int, stamp: str) -> tuple[int, int]:
    """(rows, teams) actually readable at a stamp — read back, never assumed."""
    rows = d1_store.query(
        "SELECT COUNT(*) AS n, COUNT(DISTINCT subject_id) AS t FROM stat_observations "
        "WHERE season = ? AND week = ? AND subject_type = 'team' AND recorded_at = ?",
        [int(season), int(week), str(stamp)])
    if not rows:
        return 0, 0
    return int(rows[0].get("n") or 0), int(rows[0].get("t") or 0)


@_guard
def snapshot_team_analytics(teams, keys=None, season: int = 0, week: int | None = None,
                            source: str = "cfbd", publish: bool = True,
                            identity: dict | None = None,
                            authoritative_pull: bool = False) -> int:
    """Archive per-team season analytics into stat_observations. Returns rows written.

    One stamp for the whole pull (so the pull has an identity the marker can name), then --
    only when every chunk has confirmed -- ONE publication record naming that stamp. A pull
    whose readable rows do not cover what it wrote is NOT published, so a later partial
    writer can never displace it.
    """
    rows = team_analytics_rows(teams, keys, season, week, source=source)
    if not rows:
        return 0
    stamp = _now()
    for r in rows:
        r["recorded_at"] = stamp
    n = d1_store.upsert_stat_observations_bulk(rows)
    if not n:
        return 0
    if publish:
        # QA §2: the marker is written ONLY by the authoritative full-pull coordinator, and
        # ONLY when the pull's manifest is complete and the read-back matches it exactly.
        man = _pull_manifest(rows)
        reasons = _manifest_reasons(man)
        if not authoritative_pull:
            reasons.append("caller is not the authoritative full-pull coordinator")
        if reasons:
            print(f"[d1_write_path] NOT publishing {season} wk{week}: " + "; ".join(reasons))
            return n
        # D1's meta.rows_written counts INDEX maintenance, not rows: measured on a scratch
        # database, a 2-row stat_observations insert reports 7 (one row + both indexes).
        # Completeness is therefore checked with READ-BACK ROWS against the rows this pull
        # intended to write. Comparing against `n` would refuse to publish forever on real D1
        # -- exactly the kind of provider-specific defect SQLite cannot show.
        expected = len(rows)
        got_rows, got_teams = _selection_counts(season, week, stamp)
        if got_rows < expected:
            print(f"[d1_write_path] NOT publishing {season} wk{week}: intended {expected} rows, "
                  f"only {got_rows} readable at stamp {stamp}")
            return n
        # QA §2 + counterexample 4: verify the EXACT row set the pull intended against what is
        # readable at the stamp -- values included, and an EXTRA row at the stamp is a
        # mismatch too (a partial writer sharing the stamp must not be silently adopted).
        exp = {}
        for r in rows:
            exp[(r.get("subject_id"), str(r.get("stat_key")))] = r.get("value")
        got = {}
        for r in _read_back_rows(season, week, stamp, expected):
            got[(r.get("subject_id"), str(r.get("stat_key")))] = r.get("value")
        missing = [k for k in exp if k not in got]
        extra = [k for k in got if k not in exp]
        wrong = [k for k in exp if k in got and not d1_store._same_value(got[k], exp[k])]
        if missing or extra or wrong:
            print(f"[d1_write_path] NOT publishing {season} wk{week}: read-back disagrees "
                  f"({len(missing)} missing, {len(extra)} extra, {len(wrong)} wrong value)"
                  + (f"; e.g. missing={missing[:2]} extra={extra[:2]} wrong={wrong[:2]}"
                     if (missing or extra or wrong) else ""))
            return n
        # QA round 3: publish a digest of the EXACT values that were verified, so a reader can
        # prove later that the stored snapshot is still the one the marker names.
        publish_analytics_pull(season, week, stamp, got_rows, got_teams, identity,
                               keys=man["keys"],
                               digest=_value_digest((k[0], k[1], v) for k, v in exp.items()))
    return n


def archived_analytics_weeks(season: int) -> list[int]:
    """Weeks holding archived team analytics for a season (newest first)."""
    if not read_enabled():
        return []
    try:
        rows = d1_store.query(
            "SELECT week, COUNT(*) AS n FROM stat_observations "
            "WHERE season = ? AND subject_type = 'team' GROUP BY week ORDER BY week DESC",
            [int(season)])
        return [int(r["week"]) for r in (rows or []) if int(r.get("n") or 0) > 0]
    except Exception as e:  # noqa: BLE001
        print(f"[d1_write_path] archived_analytics_weeks failed: {e}")
        return []


def _fold_rows(rows, want) -> list[dict]:
    """Fold (team, stat_key, value) rows into one dict per team."""
    by_tid: dict = {}
    for r in rows or []:
        k = r.get("k")
        if want is not None and k not in want:
            continue
        rec = by_tid.setdefault(r.get("tid"), {"team_id": r.get("tid"), "name": r.get("name")})
        rec[k] = r.get("v")
    return list(by_tid.values())


def _select_at_stamp(season: int, week: int, stamp: str, want):
    return _fold_rows(d1_store.query(
        "SELECT o.subject_id AS tid, o.stat_key AS k, o.value AS v, t.name AS name "
        "FROM stat_observations o LEFT JOIN teams t ON t.team_id = o.subject_id "
        "WHERE o.season = ? AND o.week = ? AND o.subject_type = 'team' AND o.recorded_at = ?",
        [int(season), int(week), str(stamp)]), want)


def _select_raw_at_stamp(season: int, week: int, stamp: str) -> list[dict]:
    """The stamp's rows UNFOLDED, so they can be digested before being served."""
    return d1_store.query(
        "SELECT o.subject_id AS tid, o.stat_key AS k, o.value AS v, t.name AS name "
        "FROM stat_observations o LEFT JOIN teams t ON t.team_id = o.subject_id "
        "WHERE o.season = ? AND o.week = ? AND o.subject_type = 'team' AND o.recorded_at = ?",
        [int(season), int(week), str(stamp)]) or []


def published_values_digest(season: int, pub: dict | None = _UNSET) -> str | None:
    """Fingerprint of the values CURRENTLY stored at the published stamp.

    The marker's own `digest` records what was verified AT PUBLICATION; this records what is
    there NOW. Any cache of a payload derived from those rows must key on THIS, or drift under
    an unchanged marker is served from a warm hit and the reader's check never runs.

    Selects only the three digest fields (no team join), so it is cheaper than a serve read.
    """
    if not read_enabled():
        return None
    pub = analytics_publication(season) if pub is _UNSET else pub
    if not pub or not pub.get("stamp"):
        return None
    try:
        wk, stamp = int(pub.get("week")), str(pub.get("stamp"))
    except (TypeError, ValueError):
        return None
    rows = d1_store.query(
        "SELECT subject_id AS tid, stat_key AS k, value AS v FROM stat_observations "
        "WHERE season = ? AND week = ? AND subject_type = 'team' AND recorded_at = ?",
        [int(season), wk, stamp]) or []
    if not rows:
        return None
    return _value_digest((r.get("tid"), r.get("k"), r.get("v")) for r in rows)


def load_team_analytics(season: int, week: int | None = None,
                        keys: list | tuple | None = None,
                        pub: dict | None = _UNSET) -> list[dict]:
    """SERVING door — the published COMPLETE pull only (Task 4/9).

    Selection is the publication record's stamp, never `MAX(recorded_at)`: the archive is
    append-only, so a newer stamp can belong to a single-row poll, which under the old rule
    became the whole served payload. An absent, malformed, count-mismatched or different-week
    record yields [] so the caller keeps its own fallback (the disk file) — a partial or
    unmarked selection is never promoted to visitors.
    """
    if not read_enabled():
        return []
    # Task 3: accept a record the caller already read, so ONE request performs ONE marker
    # read instead of one per consumer (_served_analytics and this function both need it).
    pub = analytics_publication(season) if pub is _UNSET else pub
    if not pub:
        return []
    try:
        wk, stamp = int(pub.get("week")), str(pub.get("stamp") or "")
        if week is not None and int(week) != wk:
            return []
        if not stamp:
            return []
        n_rows, n_teams = _selection_counts(season, wk, stamp)
        if n_rows != int(pub.get("n_rows") or -1) or n_teams != int(pub.get("n_teams") or -1):
            print(f"[d1_write_path] publication for {season} wk{wk} does not match the table "
                  f"(marker {pub.get('n_rows')}/{pub.get('n_teams')}, read back "
                  f"{n_rows}/{n_teams}); serving the fallback instead")
            return []
        rows = _select_raw_at_stamp(season, wk, stamp)
        # QA round 3: counts are not integrity. A value changed under the SAME stamp keeps the
        # row and team counts identical -- an offsetting +5/-5 pair leaves even the sums equal --
        # so the marker alone cannot vouch for what is stored. Recompute the digest the
        # publisher verified and refuse to serve a snapshot that no longer matches it.
        marker_digest = str(pub.get("digest") or "")
        if marker_digest:
            got_digest = _value_digest((r.get("tid"), r.get("k"), r.get("v")) for r in rows)
            if got_digest != marker_digest:
                print(f"[d1_write_path] publication for {season} wk{wk} FAILED its value digest: "
                      "the stored snapshot is not the one the marker names; serving the fallback "
                      "instead")
                return []
        else:
            # A marker written before digests existed: serve it, but never call it verified.
            print(f"[d1_write_path] publication for {season} wk{wk} carries no value digest "
                  "(predates it): serving UNVERIFIED")
        return _fold_rows(rows, set(keys) if keys else None)
    except Exception as e:  # noqa: BLE001
        print(f"[d1_write_path] load_team_analytics failed: {e}")
        return []


def load_team_analytics_raw(season: int, week: int, keys: list | tuple | None = None) -> list[dict]:
    """HISTORICAL/backtest door — newest stamp for an EXPLICIT week, marker or not.

    Kept because report tooling and backtests read archived seasons that predate the
    publication record. This is NOT the serving path: unmarked data must never reach a
    visitor, which is exactly why the two doors are separate functions.
    """
    if not read_enabled():
        return []
    want = set(keys) if keys else None
    try:
        if d1_store.stat_obs_append_only():
            rows = d1_store.query(
                "SELECT o.subject_id AS tid, o.stat_key AS k, o.value AS v, t.name AS name "
                "FROM stat_observations o LEFT JOIN teams t ON t.team_id = o.subject_id "
                "WHERE o.season = ? AND o.week = ? AND o.subject_type = 'team' "
                "AND o.recorded_at = (SELECT MAX(recorded_at) FROM stat_observations "
                "                     WHERE season = ? AND week = ? AND subject_type = 'team')",
                [int(season), int(week), int(season), int(week)])
        else:
            rows = d1_store.query(
                "SELECT o.subject_id AS tid, o.stat_key AS k, o.value AS v, t.name AS name "
                "FROM stat_observations o LEFT JOIN teams t ON t.team_id = o.subject_id "
                "WHERE o.season = ? AND o.week = ? AND o.subject_type = 'team'",
                [int(season), int(week)])
        return _fold_rows(rows, want)
    except Exception as e:  # noqa: BLE001
        print(f"[d1_write_path] load_team_analytics_raw failed: {e}")
        return []


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


def _wx(g: dict) -> dict:
    """Weather block on a game row, under either name the callers use."""
    wx = g.get("weather") or g.get("wx") or {}
    return wx if isinstance(wx, dict) else {}


def _wx_num(g: dict, key: str):
    v = _wx(g).get(key)
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _pre_kickoff(g: dict):
    """Return the parsed kickoff when the game is provably still upcoming, else None.

    Shared by every pre-kickoff write so the D2 no-hindsight rule is enforced in ONE
    place. Unparsable or absent kickoff => None (refuse), because "we cannot prove this
    is pre-game" and "this is pre-game" are not the same claim.
    """
    kick = g.get("kickoff") or g.get("date") or g.get("kickoff_ts")
    if not kick:
        return None
    try:
        when = datetime.fromisoformat(str(kick).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when if when > datetime.now(timezone.utc) else None


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
                     "win_prob_home": wp,
                     # Part 4: persist the weather the model actually consumed. CFBD
                     # /games/weather is a live lookup with no history, so this row is
                     # the only place this game's wind/temp/condition will ever exist.
                     "wind_mph": _wx_num(g, "wind"),
                     "temp_f": _wx_num(g, "temp"),
                     "condition": (_wx(g).get("condition") or None),
                     "indoor": (1 if _wx(g).get("indoor") else 0) if _wx(g) else None,
                     "wind_penalty": g.get("wind_penalty")})
    if rejected:
        print(f"[d1_write_path] predictions: {rejected} row(s) REFUSED (kickoff already passed "
              f"or unknown — D2 no-hindsight guard)")
    if not rows:
        return 0
    return d1_store.insert_model_predictions(rows)


# ── Results (D1 `games`) — the durable store for finals (finding F1) ──────────
# Same field mapping as scripts/backfill_d1.py::do_games and scripts/refresh_d1_games.py.
# Two mappings that drift is how a dataset goes inconsistent, so keep them identical.

def snapshot_games(games: list) -> int:
    """Archive fetched games (finals AND schedule) into D1 `games`. Write door for results."""
    rows = [{"game_id": g["id"], "season": g.get("season"), "week": g.get("week"),
             "home_id": g.get("homeId"), "away_id": g.get("awayId"),
             "kickoff": g.get("startDate"), "home_score": g.get("homePoints"),
             "away_score": g.get("awayPoints"),
             "status": "final" if g.get("completed") else "scheduled",
             "venue": g.get("venue"), "neutrality": 1 if g.get("neutralSite") else 0}
            for g in (games or []) if g.get("id")]
    if not rows:
        return 0
    return d1_store.upsert_games(rows)


def load_games(season: int, completed_only: bool = True) -> list[dict]:
    """Games for a season with team NAMES resolved. Read door for the results dataset.

    Resolving home/away to names here keeps the join in the data layer instead of every
    caller re-deriving it (and the served payload is name-keyed).
    """
    if not read_enabled():
        return []
    sql = ("SELECT g.game_id, g.season, g.week, g.kickoff, g.status, g.home_score, "
           "       g.away_score, h.name AS home, a.name AS away "
           "FROM games g LEFT JOIN teams h ON h.team_id = g.home_id "
           "LEFT JOIN teams a ON a.team_id = g.away_id WHERE g.season = ?")
    if completed_only:
        sql += " AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL"
    try:
        return d1_store.query(sql, [season])
    except Exception as e:  # noqa: BLE001
        print(f"[d1] load_games failed: {e}")
        return []


def load_state(key: str) -> str | None:
    """Read a durable app_state value from D1. None when disabled/absent/unreadable."""
    if not read_enabled():
        return None
    try:
        return d1_store.get_app_state(key)
    except Exception as e:  # noqa: BLE001
        print(f"[d1_write_path] state load failed for {key}: {e}")
        return None


def save_state(key: str, value: str) -> int:
    """Write a durable app_state value. Returns rows written (0 on any failure)."""
    if not enabled():
        return 0
    try:
        return d1_store.set_app_state(key, value)
    except Exception as e:  # noqa: BLE001
        print(f"[d1_write_path] state save failed for {key}: {e}")
        return 0


def store_slate(season: int, week: int, fingerprint: str, model_version: str,
                payload_json: str, kind: str = "schedule") -> int:
    """Persist a derived board (schedule slate | rankings | win_totals).

    Not @_guard-wrapped: the caller wants the row count and a failure is already
    contained (it returns 0 and the page falls back to computing).
    """
    try:
        return d1_store.save_slate(int(season), int(week), fingerprint,
                                   model_version, payload_json, kind=kind)
    except Exception as e:  # noqa: BLE001
        print(f"[d1_write_path] board store failed ({kind}): {e}")
        return 0


def load_slate(season: int, week: int, kind: str = "schedule") -> dict | None:
    """Read a stored board. None when absent, disabled, or unreadable."""
    if not read_enabled():
        return None
    try:
        return d1_store.load_slate(int(season), int(week), kind=kind)
    except Exception as e:  # noqa: BLE001
        print(f"[d1_write_path] board load failed ({kind}): {e}")
        return None


@_guard
def snapshot_weather(games: list[dict], season: int | None = None,
                     week: int | None = None) -> int:
    """weather_snapshots — one row per game per poll, PRE-KICKOFF ONLY.

    Why this exists: `/games/weather` is a live lookup with no historical endpoint, so
    a game's conditions are gone once the season ends. model_predictions carries
    wind/temp/condition, but only for games that happened to get a prediction row —
    the rest of the slate was never recorded. This is the dedicated time series the
    backtest grows on: every game, every poll, from here forward.

    Same D2 no-hindsight rule as the other streams: a reading taken after kickoff is
    not a forecast, and letting it in would poison the dataset while looking healthy.

    Values come from the SAME parser the model consumes (`_wx` / `_wx_num` read
    whatever `_cfbd_weather` produced) — this table cannot disagree with the model.

    `games` carry: game_id (or id), kickoff/date (ISO UTC), season, week, and the
    weather block under 'weather' (or 'wx'). A game with no weather block is simply
    not written: absence of a reading is not a reading.
    """
    poll_ts = datetime.now(timezone.utc).isoformat()
    rows = []
    skipped_started = 0
    for g in games:
        gid = g.get("game_id") or g.get("id")
        if not gid:
            continue
        if _pre_kickoff(g) is None:
            skipped_started += 1
            continue
        wx = _wx(g)
        if not wx:
            continue
        rows.append({
            "game_id": gid,
            "season": g.get("season", season),
            "week": g.get("week", week),
            "kickoff_utc": str(g.get("kickoff") or g.get("date") or "") or None,
            "poll_ts": poll_ts,
            "wind_mph": _wx_num(g, "wind"),
            "temp_f": _wx_num(g, "temp"),
            "condition": (wx.get("condition") or None),
            "indoor": (1 if wx.get("indoor") else 0),
        })
    if skipped_started:
        print(f"[d1_write_path] weather: {skipped_started} game(s) skipped "
              f"(kickoff already passed — D2 no-hindsight guard)")
    if not rows:
        return 0
    return d1_store.insert_weather_snapshots(rows)


@_guard
def snapshot_injuries(games: list[dict], model_version: str = "composite") -> int:
    """injury_snapshots — ONE ROW PER TEAM PER GAME, written before kickoff (Part 2).

    Why this exists: CFBD has no point-in-time injury feed. /games/teams gives injuries
    as of NOW, so once a season is over there is no way to reconstruct what was known
    before any given kickoff — which means the star-player adjustment can never be
    backtested. Recording it live is the only way that test ever becomes possible.

    Same D2 no-hindsight guard as model_predictions, and for the same reason: a row
    written after the result would silently look like a prediction and corrupt the very
    dataset it is meant to create. Rows carry NULL actual_*/residual_* — a later
    reconciliation pass fills those once the game is final.

    `games` carry the enriched serve-time dicts: game_id, kickoff/date, season, week,
    home/away (+ home_proj/away_proj or predicted_margin_home), the *_injury_adj values
    and the injury lists.
    """
    rows = []
    rejected = 0
    for g in games:
        gid = g.get("game_id") or g.get("id")
        if not gid:
            continue
        if _pre_kickoff(g) is None:
            rejected += 1
            continue
        kick = g.get("kickoff") or g.get("date") or g.get("kickoff_ts")
        total = g.get("predicted_total", g.get("total"))
        home = g.get("home") or g.get("home_team")
        away = g.get("away") or g.get("away_team")
        hm = g.get("predicted_margin_home")
        if hm is None:
            hp, ap = g.get("home_proj"), g.get("away_proj")
            hm = (hp - ap) if (hp is not None and ap is not None) else None
        for team, opp, adj, lst, sign in (
            (home, away, g.get("home_injury_adj"), g.get("home_injuries"), 1.0),
            (away, home, g.get("away_injury_adj"), g.get("away_injuries"), -1.0),
        ):
            if not team:
                continue
            rows.append({
                "game_id": gid,
                "season": g.get("season"),
                "week": g.get("week"),
                "team": team,
                "opponent": opp,
                "kickoff_ts": str(kick),
                "injury_list": lst or [],
                "injury_adj_applied": adj,
                # Margin from THIS team's point of view, so the sign is self-describing.
                "predicted_margin": (hm * sign) if hm is not None else None,
                "predicted_total": total,
                "model_version": model_version,
            })
    if rejected:
        print(f"[d1_write_path] injuries: {rejected} game(s) REFUSED (kickoff already "
              f"passed or unknown — D2 no-hindsight guard)")
    if not rows:
        return 0
    return d1_store.insert_injuries(rows)


@_guard
def snapshot_raw_payload(endpoint: str, payload) -> int:
    """Archive a verbatim upstream response (raw_payloads) — Part 3.

    `endpoint` doubles as the idempotency key, so callers pass a dated key
    (e.g. 'propline/odds_full/2026-09-23') and a re-run inside the same day replaces
    rather than duplicates.
    """
    return d1_store.append_raw_payloads([{"endpoint": endpoint, "payload": payload}])


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


# Key for the durable "last successful analytics pull" timestamp.
_LAST_PULL_KEY = "last_analytics_pull_ts"


def get_last_pull_ts() -> float | None:
    """Durable last-pull timestamp, or None when unknown.

    Deliberately NOT gated by D1_WRITE_ENABLED and never raising: this is a READ, and
    the caller falls back to the (unreliable, image-baked) file. Returns None rather
    than 0.0 so "unknown" is distinguishable from "epoch".
    """
    try:
        v = d1_store.get_app_state(_LAST_PULL_KEY)
        return float(v) if v else None
    except Exception as e:  # noqa: BLE001 — a read failure must not break the site
        print(f"[d1_write_path] durable last-pull read failed (falling back to file): {e}")
        return None


@_guard
def set_last_pull_ts(ts: float) -> int:
    """Persist the last successful pull timestamp durably (survives recycles)."""
    return d1_store.set_app_state(_LAST_PULL_KEY, repr(float(ts)))