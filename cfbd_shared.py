#!/usr/bin/env python3
"""cfbd_shared.py — THE single CFBD client, shared by the live app and the D1 backfill.

Why this exists (CEO directive: no parallel implementations): backfill_d1.py needs
auth, HTTP, retry and team-name matching identical to the site's, but it must NOT
import app.py (importing app runs `_start_scheduler()` at module level and would
start a second live poller). So the CFBD path lives here, side-effect free, and
BOTH app.py and the backfill import it.

No module-level side effects: no scheduler, no threads, no data loading.
"""
from __future__ import annotations

import os
import random
import time

import httpx

CFBD_BASE = "https://api.collegefootballdata.com"
CFBD_YEAR = 2026
CFBD_YEAR_FALLBACK = 2025

_CLIENT = httpx.Client(timeout=15, limits=httpx.Limits(max_connections=20))

# ── /teams is a STATIC list; never re-fetch it per lookup ────────────────────
# team_aliases() used to call cfbd_get("teams") on EVERY invocation. compute_win_totals()
# resolves one FCS opponent per game (127 of them in 2026), so a single Win Totals rebuild
# issued 128 live CFBD round-trips and spent ~48s in socket reads alone — the real reason
# that endpoint could blow past a 60s timeout, and ~3,000 wasted CFBD calls/day.
# The list changes at most yearly, so a process-local TTL cache is safe: identical data,
# just not re-downloaded. (KEEP the TTL modest: CFBD adds FCS schools as the season starts.)
_TEAMS_CACHE: dict[int, dict] = {}
TEAMS_TTL = 6 * 3600


def _teams_cached(year: int) -> list[dict]:
    """CFBD /teams for `year`, cached in-process for TEAMS_TTL seconds."""
    hit = _TEAMS_CACHE.get(year)
    if hit and time.time() - hit["ts"] < TEAMS_TTL:
        return hit["teams"]
    if hit and not hit["teams"]:
        return hit["teams"]                       # a failed fetch: don't hammer the API
    data = cfbd_get("teams", year=year) or []
    if data:
        _TEAMS_CACHE[year] = {"ts": time.time(), "teams": data}
    else:
        _TEAMS_CACHE.setdefault(year, {"ts": time.time(), "teams": []})
    return data


def _key() -> str:
    """CFBD key from env, falling back to the repo .env. Self-sufficient so callers
    (the app OR the backfill) never have to remember to load .env first."""
    k = os.environ.get("CFBD_API_KEY", "")
    if k:
        return k
    try:
        env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        for line in open(env, encoding="utf-8"):
            if line.startswith("CFBD_API_KEY="):
                k = line.split("=", 1)[1].strip().strip('"').strip("'")
                os.environ["CFBD_API_KEY"] = k
                return k
    except OSError:
        pass
    return ""


def cfbd_headers() -> dict:
    return {"Authorization": f"Bearer {_key()}", "User-Agent": "cfb-analytics/1.0"}


def _meter(resp) -> None:
    """Count one CFBD call and capture the provider's own quota reading (Phase 3.5).

    Wrapped so metering can never break a data call: an error here is swallowed.
    """
    try:
        import budget  # noqa: PLC0415 — optional dependency, never fatal
        budget.record("cfbd", 1)
        budget.note_headers("cfbd", dict(resp.headers))
    except Exception:  # noqa: BLE001
        pass


def cfbd_get(endpoint: str, year: int = CFBD_YEAR, retries: int = 3,
             base_delay: float = 1.0, **extra) -> list:
    """GET from CFBD with auth + exponential backoff. Returns a list ([] on failure).

    `extra` carries additional query params (week, seasonType, team, ...) so
    historical ranged pulls use the SAME client as the live site.
    """
    url = f"{CFBD_BASE}/{endpoint}"
    params = {"year": year, **extra}
    # Degraded mode (D1_RISK_REGISTER A4): at/above the pause threshold, STOP
    # calling and let callers fall back to cached/D1 data — exhausted must mean
    # stale-but-honest, never broken.
    try:
        import budget  # noqa: PLC0415
        if not budget.should_call("cfbd"):
            st = budget.status("cfbd")
            print(f"[cfbd_shared] CFBD paused by budget ({st['pct']}% of {st['period']} cap) "
                  f"— {endpoint} served from cache")
            return []
    except Exception:  # noqa: BLE001
        pass
    last = None
    for attempt in range(retries):
        try:
            import budget  # noqa: PLC0415
            budget.record("cfbd", 1)   # every attempt costs a request, even a 404
        except Exception:  # noqa: BLE001
            pass
        try:
            resp = _CLIENT.get(url, headers=cfbd_headers(), params=params)
            _meter(resp)
            if resp.status_code == 404:
                return []
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else [data]
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt < retries - 1:
                time.sleep(base_delay * (2 ** attempt) + random.uniform(0, 0.5))
    print(f"[cfbd_shared] {url} failed after {retries} retries: {last}")
    return []


def teams_by_name(year: int = CFBD_YEAR) -> dict:
    """All teams keyed by school name -> team dict (has 'id'). Same matcher the
    live site uses, so name->ID is identical in the app and the archive."""
    data = _teams_cached(year)
    if not data:
        data = _teams_cached(CFBD_YEAR_FALLBACK)
    return {t["school"]: t for t in data if t.get("school")}


def team_aliases(year: int = CFBD_YEAR) -> dict:
    """{any-name-or-alias: team_id} for NAME->ID matching in the archive.

    CFBD's ratings/stats endpoints key by team NAME, and some rows use an ALIAS the
    `school` field does not carry (live examples: ratings say "Albany" while /teams
    says "UAlbany"; "Southeastern Louisiana" vs "SE Louisiana"; "UTRGV" vs
    "UT Rio Grande Valley"). Those rows were silently skipped. Canonical `school`
    names always win (setdefault), so this is additive and cannot re-point a team
    that already matched. Deliberately a SEPARATE function: teams_by_name() stays
    exactly as the live app expects (adding alias keys there would duplicate teams
    for any caller that iterates its values)."""
    out: dict[str, int] = {}
    data = _teams_cached(year) or _teams_cached(CFBD_YEAR_FALLBACK) or []
    for t in data:
        if not t.get("id"):
            continue
        if t.get("school"):
            out.setdefault(t["school"], t["id"])
    for t in data:
        if not t.get("id"):
            continue
        for alias in (t.get("alternateNames") or []):
            if alias and alias not in out:
                out[alias] = t["id"]
    # Names whose canonical school is itself the alias form the endpoints emit.
    for alias, canonical in (("Albany", "UAlbany"),
                             ("Southeastern Louisiana", "SE Louisiana"),
                             ("UTRGV", "UT Rio Grande Valley")):
        if canonical in out:
            out.setdefault(alias, out[canonical])
    return out