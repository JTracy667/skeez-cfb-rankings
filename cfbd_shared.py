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