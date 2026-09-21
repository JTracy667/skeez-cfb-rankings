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


def cfbd_get(endpoint: str, year: int = CFBD_YEAR, retries: int = 3,
             base_delay: float = 1.0, **extra) -> list:
    """GET from CFBD with auth + exponential backoff. Returns a list ([] on failure).

    `extra` carries additional query params (week, seasonType, team, ...) so
    historical ranged pulls use the SAME client as the live site.
    """
    url = f"{CFBD_BASE}/{endpoint}"
    params = {"year": year, **extra}
    last = None
    for attempt in range(retries):
        try:
            resp = _CLIENT.get(url, headers=cfbd_headers(), params=params)
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
    data = cfbd_get("teams", year=year)
    if not data:
        data = cfbd_get("teams", year=CFBD_YEAR_FALLBACK)
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
    data = cfbd_get("teams", year=year) or cfbd_get("teams", year=CFBD_YEAR_FALLBACK) or []
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