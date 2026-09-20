#!/usr/bin/env python3
"""
Nightly CFBD cache pre-warm (2:00 AM PT).

Refreshes the two CFBD caches the injury pipeline reads:
    data/cfbd_starters.json      (starting QB per team, by season passing ATT)
    data/cfbd_player_ppa.json    (season PPA per player, keyed team:::last:::initial)

Then writes the heartbeat file consumed by the COO game-day sweep:
    data/last_cache_warm.json    {ts (ISO-8601, PT), record counts, source year}

Design rules:
  * FORCE-refresh. Unlike scripts/fetch_injuries.py (which short-circuits when
    the file already exists), this script always re-pulls from CFBD — the whole
    point of a pre-warm is to replace yesterday's cache before game day.
  * NEVER overwrite a good cache with an empty/failed payload. On any fetch
    failure the on-disk file is left untouched, no heartbeat is written, and the
    script exits non-zero. An absent/stale heartbeat therefore means real
    trouble — it is never faked fresh.
  * Atomic writes (tmp + os.replace) so the live app never reads a half-written
    JSON file.
  * Stdlib only (urllib) — no httpx/app import, so it runs under any Python.
  * Read-only with respect to app.py: this script does not import app and does
    not touch model math.

Usage:
    python scripts/warm_nightly_cache.py            # refresh + write heartbeat
    python scripts/warm_nightly_cache.py --dry-run  # fetch, report, write nothing
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
STARTERS_FILE = DATA_DIR / "cfbd_starters.json"
PPA_FILE = DATA_DIR / "cfbd_player_ppa.json"
HEARTBEAT_FILE = DATA_DIR / "last_cache_warm.json"
ERROR_FILE = DATA_DIR / "last_cache_warm_error.json"

CFBD_BASE = "https://api.collegefootballdata.com"
CFBD_YEAR = 2026            # mirrors app.CFBD_YEAR
CFBD_YEAR_FALLBACK = 2025   # mirrors app.CFBD_YEAR_FALLBACK

TIMEOUT = 30
RETRIES = 3


def load_env() -> None:
    """Minimal .env loader (same behaviour as app.py: real env wins)."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[warn] could not read .env: {exc}", file=sys.stderr)


def cfbd_get(endpoint: str, params: dict, key: str):
    """GET a CFBD endpoint. Returns (status, payload) — status 'ok'/'empty'/'error'."""
    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{CFBD_BASE}/{endpoint}?{query}"
    last_err = None
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {key}",
                    "User-Agent": "cfb-analytics/1.0",
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                body = resp.read().decode("utf-8", "replace")
            data = json.loads(body) if body.strip() else []
            if not data:
                return "empty", []
            return "ok", data
        except urllib.error.HTTPError as exc:
            last_err = f"HTTP {exc.code} {exc.reason}"
            if exc.code in (401, 403):
                break  # auth problem — retrying will not help
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
        if attempt < RETRIES - 1:
            time.sleep(1.0 * (2 ** attempt))
    return "error", last_err


def clean_name(raw_name: str) -> tuple[str, str]:
    """(last_name_lower, first_initial_lower) with generational suffixes removed."""
    if not raw_name:
        return "", ""
    parts = [p.strip(".,") for p in raw_name.strip().split() if p.strip(".,")]
    if not parts:
        return "", ""
    while len(parts) > 1 and parts[-1].lower() in {"jr", "sr", "ii", "iii", "iv", "v"}:
        parts.pop()
    last = parts[-1].lower() if parts else ""
    first_initial = parts[0][0].lower() if parts and parts[0] else ""
    return last, first_initial


def fetch_starters(key: str) -> tuple[dict, int]:
    """Starting QB per team -> {team: {'qb': {...}}}. Returns (map, year_used)."""
    for year in (CFBD_YEAR, CFBD_YEAR_FALLBACK):
        status, rows = cfbd_get(
            "stats/player/season", {"year": year, "category": "passing"}, key
        )
        if status == "error":
            raise RuntimeError(f"starters fetch failed ({year}): {rows}")
        if status == "empty":
            continue
        starters: dict = {}
        for row in rows:
            if row.get("statType") != "ATT":
                continue
            team, player = row.get("team"), row.get("player")
            if not team or not player:
                continue
            try:
                att = float(row.get("stat") or 0)
            except (TypeError, ValueError):
                continue
            clean_last, first_init = clean_name(player)
            entry = starters.setdefault(team, {"qb": None})
            if not entry["qb"] or att > entry["qb"]["att"]:
                entry["qb"] = {
                    "player": player,
                    "att": att,
                    "last_name": clean_last,
                    "first_init": first_init,
                }
        if starters:
            return starters, year
    raise RuntimeError("starters fetch returned no ATT rows for any season year")


def fetch_ppa(key: str) -> tuple[dict, int]:
    """Season PPA per player -> {'team:::last:::init': ppa}. Returns (map, year_used)."""
    for year in (CFBD_YEAR, CFBD_YEAR_FALLBACK):
        status, rows = cfbd_get("ppa/players/season", {"year": year}, key)
        if status == "error":
            raise RuntimeError(f"ppa fetch failed ({year}): {rows}")
        if status == "empty":
            continue
        ppa_map: dict = {}
        for p in rows:
            team, name = p.get("team"), p.get("name") or ""
            last, init = clean_name(name)
            tot = (p.get("totalPPA") or {}).get("all") or 0.0
            try:
                tot = float(tot)
            except (TypeError, ValueError):
                continue
            if not team or not last:
                continue
            for key_tuple in ((team, last, init), (team, last, "")):
                if key_tuple not in ppa_map or tot > ppa_map[key_tuple]:
                    ppa_map[key_tuple] = round(tot, 2)
        if ppa_map:
            return {
                f"{k[0]}:::{k[1]}:::{k[2]}": v for k, v in ppa_map.items()
            }, year
    raise RuntimeError("ppa fetch returned no rows for any season year")


def write_json_atomic(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{socket.gethostname()[:0]}{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def main() -> int:
    dry_run = "--dry-run" in sys.argv[1:]
    started = time.time()
    load_env()

    key = os.environ.get("CFBD_API_KEY", "").strip()
    if not key:
        print("[warm] FATAL: CFBD_API_KEY not set in environment or .env", file=sys.stderr)
        return 2

    try:
        starters, starters_year = fetch_starters(key)
    except Exception as exc:
        return fail(f"starters: {exc}")
    try:
        ppa, ppa_year = fetch_ppa(key)
    except Exception as exc:
        return fail(f"ppa: {exc}")

    counts = {
        "cfbd_starters.json": len(starters),
        "cfbd_player_ppa.json": len(ppa),
    }

    if dry_run:
        print(f"[warm] dry-run ok: {counts} (years {starters_year}/{ppa_year})")
        return 0

    # Both caches are good — commit them together, then publish the heartbeat.
    write_json_atomic(STARTERS_FILE, starters)
    write_json_atomic(PPA_FILE, ppa)

    now_local = datetime.now().astimezone()
    now_utc = datetime.now(timezone.utc)
    heartbeat = {
        "ts": now_local.isoformat(timespec="seconds"),
        "ts_utc": now_utc.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "ok": True,
        "source": "cfbd",
        "season_year": starters_year if starters_year == ppa_year else {
            "starters": starters_year, "ppa": ppa_year
        },
        "counts": counts,
        "cfbd_starters_teams": counts["cfbd_starters.json"],
        "cfbd_player_ppa_entries": counts["cfbd_player_ppa.json"],
        "duration_seconds": round(time.time() - started, 2),
        "host": socket.gethostname(),
    }
    write_json_atomic(HEARTBEAT_FILE, heartbeat)

    if ERROR_FILE.exists():
        try:
            ERROR_FILE.unlink()
        except OSError:
            pass

    print(
        f"[warm] ok ts={heartbeat['ts']} "
        f"starters_teams={counts['cfbd_starters.json']} "
        f"ppa_entries={counts['cfbd_player_ppa.json']} "
        f"year={starters_year} in {heartbeat['duration_seconds']}s"
    )
    return 0


def fail(reason: str) -> int:
    """Record the failure; leave caches and heartbeat untouched."""
    print(f"[warm] FAILED: {reason}", file=sys.stderr)
    try:
        write_json_atomic(
            ERROR_FILE,
            {
                "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                "ok": False,
                "error": reason,
            },
        )
    except Exception:
        pass
    return 1


if __name__ == "__main__":
    sys.exit(main())