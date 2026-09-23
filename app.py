"""Skeez CFB Rankings — FastAPI backend with live data feed."""

import json
import os
import hmac
import hashlib
import re
import time
import math
import random
import threading
from collections import defaultdict
from pathlib import Path
from datetime import datetime, timedelta, timezone

import httpx

import cfbd_shared  # shared CFBD client (side-effect free) — see no-parallel-impl directive
import d1_write_path  # D1 live write-path (gated by D1_WRITE_ENABLED; never breaks the site)
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ── Shared HTTP client with retry/backoff ──
# Reused across all CFBD/PropLine/Odds API calls to avoid socket exhaustion
# and connection-pool churn from creating a new client per request.
_HTTP_CLIENT = httpx.Client(timeout=15, limits=httpx.Limits(max_connections=20))
_HTTP_CLIENT_HEADERS = {}  # set after env keys load below

# ── Paths ──
BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data" / "teams.json"
ANALYTICS_FILE = BASE_DIR / "data" / "analytics.json"
SCHEDULE_FILE = BASE_DIR / "data" / "week_schedule.json"
ACTIVE_INJURIES_FILE = BASE_DIR / "data" / "active_injuries.json"

# ── Environment (.env) support ──
def _load_env_file():
    """Minimal .env loader (no external dependency)."""
    env_file = BASE_DIR / ".env"
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
    except Exception:
        pass

_load_env_file()

# ── Pydantic models ──
class Team(BaseModel):
    model_config = {"extra": "allow"}
    rank: int
    name: str
    mascot: str
    conf: str
    emoji: str
    logo_url: str = ""
    wins: int
    losses: int
    points: float
    composite: float = 0.0
    sp_plus: float = 0.0
    recruiting_rank: int = 0
    coach_win_pct: float = 0.0
    off_ppg: float = 0.0
    off_ypp: float = 0.0
    off_3rd: float = 0.0
    def_ppg: float = 0.0
    def_ypp: float = 0.0
    def_3rd: float = 0.0
    turnover_margin: float = 0.0
    fpi_win_prob: float = 0.0
    cpi: float = 0.0
    movement: int
    streak: str
    ap_rank: int | None = None  # current AP Top 25 poll rank, or None if unranked
    coaches_rank: int | None = None  # AFCA Coaches Poll rank, or None if unranked

class RankingsResponse(BaseModel):
    week: str
    season: int
    updated: str
    teams: list[Team]
    # Which season each fallback-capable rating input was actually served from
    # (e.g. {"srs": 2025} when CFBD has not published this season's SRS). Declared
    # explicitly because this model does NOT allow extra keys, so an undeclared
    # top-level field is silently dropped — which is exactly what happened first try.
    input_vintages: dict = {}

# ── App ──
# Interactive docs are disabled on the public deploy: /openapi.json + /docs were
# serving an unauthenticated MAP of the API, including the 10 mutating POST
# endpoints (refresh/fetch/injuries-override/record-ingest). Hiding the map is
# not the fix for those endpoints lacking auth, but it removes the invitation.
app = FastAPI(title="Skeez CFB Rankings API", version="1.0.0",
              docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def _security_headers(request, call_next):
    """Baseline hardening headers on every response.

    HSTS is deliberately NOT set here — it belongs at the zone/CDN layer so it
    covers the whole domain and can be rolled back independently of the app.

    Also carries the INBOUND side of the staleness guard: every request is a chance
    to notice the served analytics data has gone stale. This deliberately does not
    depend on the refresh daemon being alive — a container can serve traffic
    indefinitely with its background thread dead or wedged, and on 2026-09-22
    nothing in the request path noticed for ~40h. The call is non-blocking (it
    spawns a thread) and can never raise into the response.
    """
    try:
        maybe_kick_staleness_guard(request.url.path)
    except Exception:
        pass
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    return response


# ── Admin gate for ops/mutating endpoints ──
# The public site is read-only by design. These routes change stored data
# (injuries, records, schedule) or force live third-party fetches, so they
# require a shared secret header. The dependency FAILS CLOSED when the secret is
# not configured: a forgotten env var must never silently re-open the endpoints.
# Nothing internal depends on the HTTP layer — the in-process scheduler calls
# these functions directly — so a misconfiguration cannot stop the app working.
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "").strip()


def require_admin(x_admin_token: str | None = Header(default=None)):
    """Reject requests without the admin token (401) or with no token set (503)."""
    if not ADMIN_TOKEN:
        raise HTTPException(503, "admin auth not configured — set ADMIN_TOKEN")
    if not x_admin_token or not hmac.compare_digest(x_admin_token, ADMIN_TOKEN):
        raise HTTPException(401, "admin token required")
    return True


_ADMIN = [Depends(require_admin)]

app.add_middleware(
    CORSMiddleware,
    # Local app — restrict to localhost/dev origins instead of wide-open "*".
    # CORS_ORIGINS env var (comma-separated) extends the list for hosted deploys
    # (e.g. Cloudflare Containers behind a custom domain or Pages frontend).
    allow_origins=[
        "http://localhost:8003",
        "http://127.0.0.1:8003",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        *[o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()],
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Cache state ──
# Separate caches so analytics fetches never pollute rankings data
_rankings_cache: dict = {}
_analytics_cache: dict = {}
_odds_cache: dict = {}   # merged odds map (PropLine + backups) — avoids 113+ API calls per page load
ODDS_CACHE_FILE = BASE_DIR / "data" / "odds_cache.json"  # disk-persisted so restarts reuse the daily fetch
LINE_HISTORY_FILE = BASE_DIR / "data" / "line_history.json"  # snapshots for line-movement detection
CACHE_TTL = 300  # 5 minutes (rankings)
ANALYTICS_TTL = 600  # 10 minutes (analytics)
ODDS_TTL = 86400  # 24 hours — live odds pulled ONCE per day, cached all other times
# Public-endpoint throttles (see /api/analytics/fetch and /api/schedule/fetch):
# these two are called by the site's own pages, so they cannot carry a secret
# header — instead a repeat call inside the TTL reuses the cached payload so a
# visitor cannot burn the CFBD/odds quota by hammering them.
ANALYTICS_FETCH_TTL = 300
SCHEDULE_FETCH_TTL = 300
_SCHEDULE_FETCH_CACHE: dict = {}   # (week, year) -> {"ts": float, "data": dict}
# Background auto-refresh: how often to wake and re-pull lines + grade results.
# Default 6 hours; 0 disables the scheduler. Env-tunable for production.
REFRESH_INTERVAL_SECONDS = int(os.environ.get("REFRESH_INTERVAL_SECONDS", 6 * 3600))

def _cache_get(store: dict, ttl: int) -> dict | None:
    if store and time.time() - store["ts"] < ttl:
        return store["data"]
    return None

def _cache_set(store: dict, data: dict):
    store["data"] = data
    store["ts"] = time.time()


def _odds_disk_cache_get() -> dict | None:
    """Load yesterday's odds from disk if still fresh (< ODDS_TTL old)."""
    try:
        if not ODDS_CACHE_FILE.exists():
            return None
        payload = json.loads(ODDS_CACHE_FILE.read_text(encoding="utf-8"))
        if time.time() - payload.get("ts", 0) < ODDS_TTL:
            raw = payload.get("odds", {})
            # Keys were stored as "home|away" strings — convert back to tuples
            return {tuple(k.split("|")): v for k, v in raw.items()}
    except Exception as e:
        print(f"[Odds] disk cache read failed: {e}")
    return None


def _odds_disk_cache_set(odds_map: dict):
    """Persist the merged odds map to disk with a timestamp (daily refresh)."""
    try:
        # Tuple keys aren't JSON-serializable — store as "home|away" strings
        serializable = {f"{k[0]}|{k[1]}": v for k, v in odds_map.items()}
        payload = {"ts": time.time(), "odds": serializable}
        ODDS_CACHE_FILE.write_text(json.dumps(payload), encoding="utf-8")
    except Exception as e:
        print(f"[Odds] disk cache write failed: {e}")


# ── Line-movement tracking + background auto-refresh ──
LINE_MOVEMENTS_FILE = BASE_DIR / "data" / "line_movements.json"  # rolling event log (CLV record)
LINE_MOVEMENTS_MAX_AGE = 7 * 86400  # keep 7 days of movement events


def _load_movement_log() -> list[dict]:
    try:
        if LINE_MOVEMENTS_FILE.exists():
            return json.loads(LINE_MOVEMENTS_FILE.read_text(encoding="utf-8")).get("movements", [])
    except Exception:
        pass
    return []


def _append_movement_log(movements: list[dict]):
    """Append detected moves to the rolling log, pruning entries older than 7 days."""
    if not movements:
        return
    log = _load_movement_log()
    log.extend(movements)
    cutoff = time.time() - LINE_MOVEMENTS_MAX_AGE
    log = [m for m in log if m.get("ts", 0) >= cutoff]
    try:
        LINE_MOVEMENTS_FILE.write_text(
            json.dumps({"ts": time.time(), "movements": log[-2000:]}, ensure_ascii=False),
            encoding="utf-8")
    except Exception as e:
        print(f"[Lines] movement log write failed: {e}")


def _snapshot_lines(odds_map: dict) -> list[dict]:
    """Detect line movement vs the previous snapshot and save a new one.

    Detected moves are appended to a rolling 7-day event log
    (data/line_movements.json) so the schedule page can show everything that
    moved recently — not just the interval since the last refresh.
    Returns the movements detected THIS pass: {home, away, market, old, new, delta}.
    """
    prev = {}
    prev_ts = 0.0
    try:
        if LINE_HISTORY_FILE.exists():
            raw = json.loads(LINE_HISTORY_FILE.read_text(encoding="utf-8"))
            prev_ts = raw.get("ts", 0)
            prev = {tuple(k.split("|")): v for k, v in raw.get("lines", {}).items()}
    except Exception:
        prev = {}
        prev_ts = 0
    movements = []
    current = {}
    now_ts = time.time()
    # Stale-baseline guard: if the previous snapshot is very old (deploy gap,
    # seeded image, restored backup), diffing against it would log a fake
    # movement for every line that moved while we were dark. Re-baseline
    # silently instead — movements only count when we've been watching.
    STALE_SNAPSHOT_SECS = 24 * 3600
    snapshot_stale = (now_ts - prev_ts) > STALE_SNAPSHOT_SECS
    for (home, away), v in odds_map.items():
        key = f"{home}|{away}"
        current[key] = {"spread": v.get("spread"), "total": v.get("total"), "book": v.get("book_title") or v.get("book")}
        old = prev.get((home, away))
        if not old or snapshot_stale:
            continue
        for market in ("spread", "total"):
            new_v = v.get(market)
            old_v = old.get(market)
            if new_v is not None and old_v is not None and new_v != old_v:
                movements.append({
                    "home": home, "away": away, "market": market,
                    "old": old_v, "new": new_v, "delta": round(new_v - old_v, 1),
                    "ts": now_ts,
                    "when": datetime.now(_PT_TZ).strftime("%a %I:%M %p") if _PT_TZ else datetime.now().strftime("%a %I:%M %p"),
                })
    # Persist: snapshot overwritten (new baseline) + events appended to the log
    try:
        LINE_HISTORY_FILE.write_text(
            json.dumps({"ts": now_ts, "lines": current}, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[Lines] history write failed: {e}")
    _append_movement_log(movements)
    return movements


# ── Weekly CFBD ratings sync (Sun–Wed 9pm PT) ──
# Once a week, re-pull CFBD's updated ratings (SP+/FPI/Elo/CPI/PPG) so the
# composite rankings and win totals reflect results as they land. The due-date
# is "most recent Sun/Mon/Tue/Wed 21:00 America/Los_Angeles"; we persist the last
# pull ts to disk so a container restart doesn't re-pull every scheduler tick
# until the next anchor (and a failed pull retries on the following tick).
try:
    from zoneinfo import ZoneInfo
    _PT_TZ = ZoneInfo("America/Los_Angeles")
except Exception:  # tzdata missing — fall back to UTC-7 (PDT) approximation
    _PT_TZ = None
WEEKLY_ANALYTICS_FILE = BASE_DIR / "data" / "last_analytics_pull.json"

# Sync anchors: Sunday 9pm PT (post-Saturday results), Monday 9pm PT (short-week
# seasons: Sunday games + Monday poll releases), Tuesday 9pm PT (CFBD's SP+/FPI
# update irregularly Sun night through Wed — Tue catches the stragglers), +
# Wednesday 9pm PT (mid-week, when most line movement happens and best-bet edges
# are widest).
# Timezone corrected ET -> PT Sep 20 2026 (Jeff): the anchor is 9pm PACIFIC.
# Under EDT the old 21:00 ET anchor fired at 6pm PT — three hours early.
_ANALYTICS_ANCHORS = ((6, 21), (0, 21), (1, 21), (2, 21))  # (weekday, hour) — Python Mon=0: Sun=6, Mon=0, Tue=1, Wed=2
# Mon anchor added Sep 6: short-week seasons (Sunday games) release polls/ratings
# Monday, and Sun->Wed left Mon/Tue results unreflected for up to 4 days.


def _most_recent_anchor_pt(now=None):
    """Most recent sync anchor (Sun/Mon/Tue/Wed 21:00 PT), inclusive of today if past."""
    if _PT_TZ is None:
        return None
    now = now or datetime.now(_PT_TZ)
    best = None
    for wd, hr in _ANALYTICS_ANCHORS:
        days_back = (now.weekday() - wd) % 7
        cand = (now - timedelta(days=days_back)).replace(
            hour=hr, minute=0, second=0, microsecond=0)
        if cand > now:
            cand -= timedelta(days=7)
        if best is None or cand > best:
            best = cand
    return best


def _load_last_analytics_pull() -> float:
    """When the analytics data was last successfully pulled.

    DURABLE SOURCE FIRST (D1 app_state). The file is only a fallback, because it is
    unreliable in two ways discovered on 2026-09-22: the container filesystem is
    ephemeral, and `COPY data/ ./data/` bakes the WORKING TREE into the image — so a
    stale local copy shipped and reported the same ~41h-old timestamp forever, no
    matter how fresh the data actually was.
    """
    try:
        ts = d1_write_path.get_last_pull_ts()
        if ts:
            return float(ts)
    except Exception as e:  # noqa: BLE001
        print(f"[analytics] durable last-pull read failed: {e}")
    try:
        if WEEKLY_ANALYTICS_FILE.exists():
            return float(json.loads(WEEKLY_ANALYTICS_FILE.read_text()).get("ts", 0))
    except Exception:
        pass
    return 0.0


def _save_last_analytics_pull(ts: float):
    """Record a successful pull. Writes D1 (truth) and the local file (fallback)."""
    try:
        d1_write_path.set_last_pull_ts(ts)
    except Exception as e:  # noqa: BLE001
        print(f"[analytics] durable last-pull write failed: {e}")
    try:
        WEEKLY_ANALYTICS_FILE.write_text(json.dumps({"ts": ts}))
    except Exception as e:
        print(f"[analytics] last-pull file write failed: {e}")


def _analytics_age_hours() -> float:
    """Age of the last successful analytics pull, in hours (None-safe -> -1.0).

    NOTE: the last-pull marker is a FILE and the container filesystem is
    ephemeral, so this resets to "never pulled" on every recycle. That is
    deliberately eager (a cold start always re-pulls) but it also means the file
    is not a durable history — which is exactly why freshness_events exists.
    """
    ts = _load_last_analytics_pull()
    if not ts:
        return -1.0
    return max(0.0, (time.time() - ts) / 3600.0)


# ── Staleness guard ─────────────────────────────────────────────────────────
# Replaces accidental self-healing. Until 2026-09-22 the app always believed it was
# due, because a stale marker was baked into the image, so every container start
# pulled. That looked like robustness but was luck — it depended on a build artifact
# being wrong, and it is exactly what masked a missed anchor for 39 hours.
#
# Now that the marker is durable, an explicit age rule is REQUIRED: without it a
# fresh container would correctly conclude "not due" and never repair stale data.
# Flag off (FRESHNESS_GUARD=0) restores the previous anchor-only behaviour.
FRESHNESS_GUARD_ON = os.environ.get("FRESHNESS_GUARD", "1").strip().lower() in (
    "1", "true", "yes", "on")
FRESHNESS_MAX_AGE_HOURS = float(os.environ.get("FRESHNESS_MAX_AGE_HOURS", "12"))


def freshness_guard_due() -> bool:
    """True when the analytics data is older than the allowed maximum age.

    Deliberately ANCHOR-INDEPENDENT: it fires on any container start, whether that
    start was a cron wake or an ordinary page view. So a missed anchor degrades to
    "stale until the next visitor" instead of a multi-day gap.

    A never-pulled state (-1.0, i.e. no durable marker) counts as due.
    """
    if not FRESHNESS_GUARD_ON:
        return False
    age = _analytics_age_hours()
    if age < 0:
        return True
    return age > FRESHNESS_MAX_AGE_HOURS


# ── Inbound staleness guard ─────────────────────────────────────────────────
# The daemon-loop guard above only gets a chance every REFRESH_INTERVAL_SECONDS
# (6h) and only while the background thread is alive. That is the wrong dependency:
# a container can serve requests indefinitely with its daemon dead or wedged, which
# is exactly how 40h of stale data went unnoticed. This path is triggered BY TRAFFIC
# instead, so freshness depends on nothing but the site being visited.
_ANALYTICS_GUARD_LOCK = threading.Lock()
_ANALYTICS_GUARD_LAST_ATTEMPT = 0.0
_ANALYTICS_GUARD_MIN_GAP_S = float(os.environ.get("FRESHNESS_GUARD_MIN_GAP_S", "600"))


def maybe_kick_staleness_guard(where: str = "request") -> bool:
    """Start a background analytics pull if the served data is stale.

    Returns True only when this call actually started a pull. Non-blocking by
    design: the request that triggered it must never wait on CFBD. Three limits
    keep it from becoming a hammer —

      * freshness_guard_due()  — only when genuinely stale (> max age, or never)
      * _ANALYTICS_GUARD_MIN_GAP_S — at most one attempt per 10 min
      * the lock — one pull at a time, and a no-op while one is in flight

    A failed attempt still records telemetry, so a guard that is failing to repair
    is visible in D1 rather than silent.
    """
    global _ANALYTICS_GUARD_LAST_ATTEMPT
    if not FRESHNESS_GUARD_ON:
        return False
    try:
        if not freshness_guard_due():
            return False
    except Exception:
        return False
    now = time.time()
    if now - _ANALYTICS_GUARD_LAST_ATTEMPT < _ANALYTICS_GUARD_MIN_GAP_S:
        return False
    if not _ANALYTICS_GUARD_LOCK.acquire(blocking=False):
        return False          # a pull is already running
    _ANALYTICS_GUARD_LAST_ATTEMPT = now

    def _work():
        try:
            res = run_weekly_analytics_pull()
            if res.get("pulled"):
                _save_last_analytics_pull(time.time())
                d1_write_path.record_freshness_event(
                    "pull_success", "guard_request",
                    age_hours=_analytics_age_hours(),
                    detail=f"kicked by {where}")
            else:
                d1_write_path.record_freshness_event(
                    "pull_skip", "guard_request",
                    age_hours=_analytics_age_hours(),
                    detail=f"stale but not pulled (kicked by {where}): {str(res)[:180]}")
        except Exception as e:
            d1_write_path.record_freshness_event(
                "pull_failed", "guard_request",
                age_hours=_analytics_age_hours(), detail=str(e)[:300])
        finally:
            _ANALYTICS_GUARD_LOCK.release()

    try:
        threading.Thread(target=_work, daemon=True, name="cfb-stale-guard").start()
    except Exception:
        _ANALYTICS_GUARD_LOCK.release()
        return False
    return True


def weekly_analytics_due() -> bool:
    """True if the most recent Sun/Mon/Tue/Wed 21:00 PT anchor postdates our last pull."""
    due_at = _most_recent_anchor_pt()
    if due_at is None:
        return False
    return time.time() > due_at.timestamp() and \
        _load_last_analytics_pull() < due_at.timestamp()


def run_weekly_analytics_pull() -> dict:
    """Re-pull CFBD ratings + persist, so rankings/win-totals see fresh data.

    Mirrors POST /api/analytics/fetch (minus the HTTP layer). On failure we
    keep the existing disk cache and do NOT advance the last-pull timestamp,
    so the next scheduler tick retries."""
    summary = {"pulled": False, "teams": 0}
    try:
        analytics = fetch_live_analytics()
        if not analytics:
            print("[analytics] weekly pull returned no data; keeping existing cache")
            return summary
        _save_rating_vintages()
        with open(_CFBD_ANALYTICS_FILE, "w") as f:
            json.dump(analytics, f, indent=2)
        _enrich_with_composite(analytics)
        _cache_set(_analytics_cache, {
            "week": datetime.now().strftime("%B %d, %Y"),
            "season": CFBD_YEAR,
            "updated": datetime.now().isoformat(),
            "teams": analytics,
            "source": "cfbd",
        })
        # Rebuild the team map so schedule/rankings/win-totals see new metrics.
        _build_team_map()
        summary["pulled"] = True
        summary["teams"] = len(analytics)
        print(f"[analytics] weekly pull complete: {len(analytics)} teams")
    except Exception as e:
        print(f"[analytics] weekly pull failed: {e}")
    return summary


def _rss_mb() -> float:
    """Current process RSS in MB (for memory-peak logging). 0 if unavailable."""
    try:
        import psutil
        return psutil.Process().memory_info().rss / 1e6
    except Exception:
        return 0.0


def refresh_all() -> dict:
    """Force re-pull of live odds + final scores, then grade records.

    Used by the background scheduler and the manual /api/refresh endpoint.
    Returns a summary of what changed.
    """
    result = {"odds_refreshed": False, "graded": 0, "best_bets_graded": 0, "line_movements": []}
    mem_before = _rss_mb()
    try:
        # Forced refresh must bypass BOTH caches and hit the live feeds — busting
        # only memory would let a fresh disk cache short-circuit the fetch (the
        # hourly line-movement detection would then never see new lines).
        odds_map = _fetch_odds_live()
        result["odds_refreshed"] = bool(odds_map)
        result["line_movements"] = _snapshot_lines(odds_map)
        # D1 live write-path: append this poll to odds_snapshots (no-op unless
        # D1_WRITE_ENABLED; never raises into the refresh).
        result["d1_odds_rows"] = d1_write_path.snapshot_odds(odds_map, _normalize_team_name)
    except Exception as e:
        print(f"[refresh] odds refresh failed: {e}")
    try:
        # Bust finals cache + grade any newly-finished games (SU/ATS + best bets)
        _FINALS_CACHE["data"] = {}
        _FINALS_CACHE["ts"] = 0
        result["graded"] = _ingest_results()
        result["best_bets_graded"] = _ingest_best_bets()
    except Exception as e:
        print(f"[refresh] results grading failed: {e}")
    try:
        # D1 live write-path (no-op unless D1_WRITE_ENABLED; never raises into the refresh):
        # closing lines from the app's own CFBD source, plus a once-per-day rankings snapshot.
        result["d1_closing_rows"] = d1_write_path.snapshot_closing(
            _fetch_closing_lines_map(), _normalize_team_name)
        result["d1_rankings_rows"] = d1_write_path.daily_rankings(
            lambda: get_rankings().teams, CFBD_YEAR, None, composite_version())
    except Exception as e:
        print(f"[refresh] D1 write-path failed: {e}")
    try:
        # FCS ratings (Massey) on the same hourly cadence, so the FCS prior is
        # never stale by more than one cycle. Self-contained and non-fatal.
        result["fcs_ratings"] = refresh_fcs_ratings()
    except Exception as e:
        print(f"[refresh] FCS ratings refresh failed: {e}")
    result["ts"] = datetime.now().isoformat()
    # Memory + quota telemetry: catch spike trends before they OOM-kill the
    # service, and watch the PropLine daily budget every cycle.
    mem_after = _rss_mb()
    if mem_after:
        print(f"[refresh] memory: {mem_before:.0f}MB -> {mem_after:.0f}MB RSS")
    try:
        print(f"[refresh] {_propline_quota_str()}")
    except Exception:
        pass
    # Phase 3.5: persist the standing budget meters (D1 = ledger of record; the
    # container FS is ephemeral, so the file alone would reset on every recycle)
    # and log the burn line so "what's our burn?" is answerable from the logs too.
    try:
        import budget  # noqa: PLC0415
        budget.flush()
        print(f"[refresh] burn: {budget.burn_line()}")
    except Exception as e:  # noqa: BLE001
        print(f"[refresh] budget flush skipped: {e}")
    return result


def _served_state_items() -> list[dict]:
    """Summarise the payloads the site is CURRENTLY serving (blast-radius evidence).

    Records each endpoint's own data `as_of`, a row count, and a content hash, so a
    future staleness window can be resolved to exactly what was live instead of
    staying INCONCLUSIVE forever (which is what happened to the 2026-09-22 one).

    Deliberately limited to the two CACHED endpoints (/api/rankings, /api/analytics).
    Summarising win-totals or schedule here would issue CFBD calls on every daemon
    tick, which is not a trade worth making — and both derive from the same analytics
    source, so their staleness is bounded by the analytics `as_of` recorded here.

    Cheap to call: both sources are in-memory caches.
    """
    items: list[dict] = []

    def add(endpoint, payload, as_of=None, count=None, detail=""):
        try:
            canon = json.dumps(payload, sort_keys=True, default=str)
            items.append({
                "endpoint": endpoint,
                "as_of": str(as_of) if as_of else None,
                "row_count": count,
                "content_hash": hashlib.sha256(canon.encode("utf-8")).hexdigest()[:32],
                "detail": detail[:500],
            })
        except Exception as e:  # noqa: BLE001 — never fatal
            print(f"[served_state] {endpoint} summary failed (non-fatal): {e}")

    # /api/rankings — the composite-ordered table the site actually shows.
    # The sort check is recorded because "rankings sorted by composite" is a
    # durable correctness rule; a snapshot that silently captured an unsorted
    # table would be worth knowing about later.
    try:
        r = get_rankings()
        payload = r.model_dump() if hasattr(r, "model_dump") else r
        teams = (payload or {}).get("teams") or []
        sorted_ok = None
        try:
            sorted_ok = all(teams[i]["composite"] >= teams[i + 1]["composite"]
                            for i in range(len(teams) - 1)) if teams else None
        except Exception:
            sorted_ok = None
        add("/api/rankings", payload, (payload or {}).get("updated"), len(teams),
            f"sorted_by_composite={sorted_ok}")
    except Exception as e:  # noqa: BLE001
        print(f"[served_state] rankings unavailable (non-fatal): {e}")

    # /api/analytics — ONLY if already cached; never force a CFBD pull from here.
    try:
        cached = _cache_get(_analytics_cache, ANALYTICS_TTL)
        if cached:
            at = cached.get("teams") or []
            add("/api/analytics", cached, cached.get("updated"), len(at),
                f"source={cached.get('source')}")
    except Exception as e:  # noqa: BLE001
        print(f"[served_state] analytics cache unavailable (non-fatal): {e}")

    return items


def _start_scheduler() -> None:
    """Background daemon thread: periodically refresh lines + grade results.

    Safe for containers — no network calls at startup; the thread sleeps first.
    """
    if REFRESH_INTERVAL_SECONDS <= 0:
        return
    import threading

    def _loop():
        # Short delay only — never block the readiness probe / cold start, but
        # refresh ASAP so a baked-in data snapshot can't ship stale. (Cutover
        # requirement: pull live data immediately on container start.)
        time.sleep(10)
        # Durable proof that a container start happened, and when. Without this the
        # only evidence of a wake was a console.log that Cloudflare does not retain,
        # which is why the 2026-09-22 staleness incident was undiagnosable after the
        # fact. Recorded BEFORE any network work, so a slow or failed start still
        # leaves a trace.
        try:
            d1_write_path.record_freshness_event(
                "container_start", "scheduler", age_hours=_analytics_age_hours(),
                detail=f"refresh_interval={REFRESH_INTERVAL_SECONDS}s")
        except Exception:
            pass
        first_tick = True
        while True:
            # Failure retry: shorter sleep after a failed refresh so a transient
            # API outage doesn't leave stale lines up for a full interval.
            sleep_s = REFRESH_INTERVAL_SECONDS
            try:
                summary = refresh_all()
                if summary["graded"] or summary["line_movements"]:
                    print(f"[scheduler] refresh: {summary}")
                if not summary.get("odds_refreshed"):
                    print("[scheduler] odds refresh failed — retrying in 15 min")
                    sleep_s = min(sleep_s, 900)
            except Exception as e:
                print(f"[scheduler] refresh error: {e} — retrying in 15 min")
                sleep_s = min(sleep_s, 900)
            # On the first tick, also recompute rankings from the live ESPN feed
            # so composites reflect current data, not the baked seed snapshot.
            if first_tick:
                first_tick = False
                try:
                    if refresh_rankings_from_espn():
                        print("[scheduler] startup rankings refresh ok")
                except Exception as e:
                    print(f"[scheduler] startup rankings refresh failed: {e}")
            # Weekly CFBD ratings sync (Sun-Wed 9pm PT). Runs on the first tick
            # after it's due; a failed pull retries next tick (ts not advanced).
            try:
                anchor_due = weekly_analytics_due()
                guard_due = freshness_guard_due()
                if anchor_due or guard_due:
                    # `source` distinguishes "the scheduled anchor fired" from "the
                    # age guard caught us up" -- two different stories later.
                    src = "scheduler" if anchor_due else "guard"
                    res = run_weekly_analytics_pull()
                    if res["pulled"]:
                        _save_last_analytics_pull(time.time())
                        d1_write_path.record_freshness_event(
                            "pull_success", src, age_hours=_analytics_age_hours(),
                            detail=f"anchor_due={anchor_due} guard_due={guard_due} "
                                   f"{str(res)[:180]}")
                    else:
                        # Due, but the pull did not happen — the state that silently
                        # persisted for 39h. Recorded so it is never invisible again.
                        d1_write_path.record_freshness_event(
                            "pull_skip", src, age_hours=_analytics_age_hours(),
                            detail=f"anchor_due={anchor_due} guard_due={guard_due} "
                                   f"not pulled: {str(res)[:160]}")
            except Exception as e:
                print(f"[scheduler] weekly analytics error: {e}")
                d1_write_path.record_freshness_event(
                    "pull_failed", "scheduler", age_hours=_analytics_age_hours(),
                    detail=str(e)[:300])
            # Served-state snapshot — what the site is actually serving right now.
            # Change-driven, so it writes only when a payload differs from the last
            # state recorded today. Cheap: reads in-memory caches only.
            try:
                d1_write_path.snapshot_served_state(_served_state_items)
            except Exception as e:
                print(f"[scheduler] served-state snapshot error (non-fatal): {e}")
            time.sleep(sleep_s)

    t = threading.Thread(target=_loop, daemon=True, name="cfb-refresh")
    t.start()

# ── Daily 04:00 UTC self-restart — REMOVED (Render-era workaround) ──
# Deleted Sep 22 2026. It existed only to cap RSS ratchet on Render's 512MB
# starter plan by exiting at 04:00 UTC so Render's restart policy handed back a
# fresh container. That service is retired, and on Cloudflare Containers the hack
# was actively harmful: 04:00 UTC is exactly the Sun-Wed 21:00 PT analytics
# anchor, so the self-kill collided with the Worker cron (`0 4 * * 1,2,3,4`)
# firing the same minute and that night's pull was lost for ~4h (the watchdog
# faulted twice, and the container - being traffic-driven - had no always-on
# process to retry promptly the way Render did).
#
# If container memory ever needs bounding again, do NOT reintroduce a timed
# self-exit: Containers already recycle on sleepAfter. Use the platform's
# instance memory limit instead.

# ── Data loading ──
def load_local() -> list[dict]:
    """Load teams from local JSON file. Base fields only — analytics
    enrichment comes exclusively from cfbd_analytics.json via _build_team_map(),
    so stale analytics.json can never override fresh CFBD data."""
    try:
        with open(DATA_FILE) as f:
            return json.load(f)
    except Exception:
        return []

def _parse_espn_poll(poll: dict) -> list[dict]:
    """Parse one ESPN poll entry into the app's team-dict shape."""
    teams = []
    for rk in poll.get("ranks", []):
        team_info = rk.get("team", {})
        # location is the school name ("Ohio State"); matches CFBD names.
        name = (team_info.get("location") or team_info.get("nickname")
                or f"Team {rk.get('current')}").strip()
        try:
            points = float(rk.get("points", 0) or 0)
        except (TypeError, ValueError):
            points = 0.0
        prev = int(rk.get("previous", 0) or 0)
        cur = int(rk.get("current", 0) or 0)
        # movement > 0 = climbed; 0 = new entry / no change (UI shows —).
        movement = (prev - cur) if prev else 0
        rec = rk.get("recordSummary") or "0-0"
        try:
            w, l = (int(x) for x in str(rec).split("-"))
        except ValueError:
            w = l = 0

        teams.append({
            "rank": cur,
            "name": name,
            "mascot": team_info.get("nickname", ""),
            "conf": "FBS",
            "emoji": "🏈",
            "wins": w,
            "losses": l,
            "points": points,
            "movement": movement,
            "streak": "—",  # poll data has no game streak; leave neutral
        })
    return teams


def fetch_espn_polls() -> dict[str, list[dict]] | None:
    """
    Fetch ALL FBS polls from ESPN's public rankings API in one call.
    Returns {poll name: [team dicts]} or None on failure.

    Endpoint: /sports/football/college-football/rankings returns the AP Top 25,
    AFCA Coaches Poll and FCS Coaches Poll for the current week.
    The old /standings/ap-top-25 endpoint was retired by ESPN (404).
    """
    try:
        url = ("https://site.api.espn.com/apis/site/v2/sports/football/"
               "college-football/rankings")
        data = _http_get(url, retries=3, base_delay=1.0)
        if not data or not isinstance(data, dict):
            return None

        polls = {p.get("name"): p for p in data.get("rankings", [])}
        out: dict[str, list[dict]] = {}
        for name in ("AP Top 25", "AFCA Coaches Poll"):
            if name in polls:
                teams = _parse_espn_poll(polls[name])
                if teams:
                    out[name] = teams
        return out or None

    except Exception as e:
        print(f"[ESPN fetch failed] {e}")
        return None


def fetch_espn_poll(poll_name: str = "AP Top 25") -> list[dict] | None:
    """Convenience wrapper: one poll by name (default AP Top 25)."""
    polls = fetch_espn_polls()
    if not polls:
        return None
    return polls.get(poll_name) or None


# Polls change weekly — cache an hour so page loads don't hammer ESPN.
_espn_polls_cache: dict = {}
AP_POLL_TTL = 3600

_AP_NAME = "AP Top 25"
_COACHES_NAME = "AFCA Coaches Poll"


def _poll_rank_map(poll_name: str) -> dict[str, int]:
    """{team name (lowercase): rank} for one poll.

    Empty dict on any failure — callers must treat a missing key as
    'not ranked' and never let an ESPN outage break the rankings page."""
    cached = _cache_get(_espn_polls_cache, AP_POLL_TTL)
    if not cached:
        polls = fetch_espn_polls()
        if polls:
            _cache_set(_espn_polls_cache, polls)
            cached = polls
    return {t["name"].lower(): t["rank"]
            for t in (cached or {}).get(poll_name, [])}


def _ap_rank_map() -> dict[str, int]:
    return _poll_rank_map(_AP_NAME)


def _coaches_rank_map() -> dict[str, int]:
    return _poll_rank_map(_COACHES_NAME)

def _rating_vintages(teams) -> dict:
    """Which season each fallback-capable rating input was actually served from.

    Three sources, weakest first, each overriding the previous:
      1. data/rating_vintages.json — durable record, refreshed by the fetch path, and
         the only source available when the served rows predate the vintage field.
      2. the served rows themselves, which carry `srs_source_year` after a pull.
      3. this process's fetch helpers — authoritative for a live pull.
    Returns {} when unknown, so the UI shows NO vintage label rather than a wrong one.
    Never guesses: a wrong label is worse than no label.
    """
    vintages: dict = {}
    try:
        with open(BASE_DIR / "data" / "rating_vintages.json") as f:
            rec = json.load(f)
        vintages = {k: v for k, v in rec.items() if isinstance(v, int)}
    except Exception:
        vintages = {}
    # Rows beat the file: the rows are the data actually being served right now, while
    # the file only records what some earlier pull saw. (Caught by the selftest — the
    # first version had this backwards and silently reported a stale vintage.)
    for t in (teams or []):
        y = t.get("srs_source_year")
        if y:
            vintages["srs"] = y
            break
    vintages.update(RATING_SOURCE_YEARS)
    return vintages


def _save_rating_vintages() -> None:
    """Persist which season each rating input was served from.

    Called by the analytics fetch path right after a pull, so a later process that only
    has the rows — or a baked seed — can still label the vintage honestly. Without this
    the knowledge dies with the process that fetched it.
    """
    if not RATING_SOURCE_YEARS:
        return
    try:
        rec = dict(RATING_SOURCE_YEARS)
        try:
            from datetime import timezone as _tz
            rec["as_of_utc"] = datetime.now(_tz.utc).isoformat(timespec="seconds")
        except Exception:
            rec["as_of_utc"] = datetime.now().isoformat(timespec="seconds")
        rec["detail"] = ("Updated by the analytics fetch path. A rating falls back to the "
                         "previous season when CFBD has not published the current one.")
        with open(BASE_DIR / "data" / "rating_vintages.json", "w") as f:
            json.dump(rec, f, indent=2)
            f.write("\n")
    except Exception:
        pass  # never let bookkeeping break a pull


def get_rankings() -> RankingsResponse:
    """Return the Skeez CFB Rankings — the composite list.

    Single source of truth: the SAME CFBD analytics dataset the Analytics
    composite tab uses, enriched with the SAME composite formula
    (project_score_multi_factor) and sorted by composite descending.
    This guarantees the main page and the composite tab always agree —
    same teams, same order, same scores.
    """
    cached = _cache_get(_rankings_cache, CACHE_TTL)
    if cached:
        return RankingsResponse(**cached)

    # Primary source: the CFBD analytics file (identical to the composite tab).
    teams = _load_cfbd_analytics_file()
    if not teams:
        # Fallbacks if the file is missing/stale.
        teams = fetch_live_analytics() or load_local()
    if not teams:
        raise HTTPException(502, "Rankings data unavailable")

    # Deep-copy so we never mutate the shared disk cache.
    teams = [dict(t) for t in teams]

    # Enrich with composite + factor contributions (identical to the composite tab).
    _enrich_with_composite(teams)

    # Sanitize None -> 0.0 for fields the Team model requires as numbers
    # (pre-season / incomplete CFBD rows carry None for these).
    _NUM_FIELDS = ("off_ppg", "off_ypp", "off_3rd", "def_ppg", "def_ypp",
                   "def_3rd", "turnover_margin", "points")
    for team in teams:
        for k in _NUM_FIELDS:
            if team.get(k) is None:
                team[k] = 0.0
        # Ensure required scalars exist for the model.
        team.setdefault("wins", 0)
        team.setdefault("losses", 0)
        team.setdefault("movement", 0)
        team.setdefault("streak", "—")
        team.setdefault("mascot", "")
        team.setdefault("conf", "FBS")
        team.setdefault("emoji", "🏈")

    # Inject cached team logos.
    for team in teams:
        logo = _LOGO_MAP.get(team.get("name", "").lower())
        if logo:
            team["logo_url"] = logo

    # Sort by composite score descending — the day's composite ranking.
    teams = sorted(teams, key=lambda t: t.get("composite", 0), reverse=True)
    # Main page shows the TOP 25 composite teams only.
    # (The full list lives on the Analytics composite tab.)
    teams = teams[:25]
    # Re-assign rank to reflect composite order.
    for i, t in enumerate(teams, 1):
        t["rank"] = i

    # AP Top 25 + AFCA Coaches cross-reference: each team's current poll rank
    # (or None if not ranked). ESPN outage => empty map => everyone shows "—",
    # never an error.
    ap_map = _ap_rank_map()
    coaches_map = _coaches_rank_map()
    for t in teams:
        key = t.get("name", "").lower()
        t["ap_rank"] = ap_map.get(key)
        t["coaches_rank"] = coaches_map.get(key)

    # Live W/L overlay: records change after every game — far more often than
    # the twice-weekly CFBD analytics pull. CFBD outage => no-op (stale but
    # nonzero beats resetting everyone to 0-0).
    _overlay_records(teams)

    result = {
        "week": datetime.now().strftime("%B %d, %Y"),
        "season": datetime.now().year,
        "updated": datetime.now().isoformat(),
        "teams": teams,
        "input_vintages": _rating_vintages(teams),
    }
    _cache_set(_rankings_cache, result)
    return RankingsResponse(**result)

# ── API Routes ──
@app.get("/api/rankings")
def api_rankings():
    """Get current power rankings."""
    return get_rankings()

@app.get("/api/rankings/search")
def api_search(q: str):
    """Search teams by name, mascot, or conference."""
    data = get_rankings()
    q_lower = q.lower()
    results = [
        t for t in data.teams
        if q_lower in t.name.lower()
        or q_lower in t.mascot.lower()
        or q_lower in t.conf.lower()
    ]
    return {"query": q, "count": len(results), "teams": results}

@app.get("/api/rankings/conf/{conf}")
def api_conf(conf: str):
    """Filter teams by conference."""
    data = get_rankings()
    results = [t for t in data.teams if conf.lower() in t.conf.lower()]
    return {"conference": conf, "count": len(results), "teams": results}

@app.get("/api/rankings/{rank}")
def api_team(rank: int):
    """Get a single team by rank."""
    data = get_rankings()
    for t in data.teams:
        if t.rank == rank:
            return t
    raise HTTPException(404, f"No team at rank {rank}")

def refresh_rankings_from_espn() -> bool:
    """Warm/rebuild the rankings cache from the canonical CFBD analytics seed.

    Uses the EXACT same pipeline as GET /api/rankings (get_rankings()), so a
    container booting from a baked snapshot immediately serves the same
    enriched composites production serves.

    BUGFIX: the previous implementation recomputed from the bare ESPN poll
    stub and cached that 25-field payload, dropping every CFBD metric
    (SP+/Elo/FPI/talent/havoc). Because the startup scheduler tick called it,
    a container refreshed on boot served degraded numbers until the 300s cache
    expired. Rebuild from data/cfbd_analytics.json instead (the rankings page's
    source of truth); the live ESPN poll still feeds the AP/Coaches columns via
    get_rankings()."""
    _rankings_cache.clear()
    try:
        get_rankings()
        return True
    except Exception as e:
        print(f"[rankings refresh] {e}")
        return False


@app.post("/api/rankings/refresh", dependencies=_ADMIN)
def api_refresh():
    """Force-refresh the ESPN data feed, computing composites."""
    if refresh_rankings_from_espn():
        return {"status": "refreshed", "source": "espn", "teams": 25}
    else:
        return {"status": "fallback", "source": "local", "note": "ESPN fetch failed, using local data"}

@app.get("/api/health")
def api_health():
    """Health check.

    `build` is the deployed image tag (BUILD_TAG, set by the Worker from the
    image tag in wrangler.jsonc). It exists so a post-deploy check can tell
    "the new image is actually serving" apart from "the old warm instance is
    still answering" — a warm container can keep serving the previous image for
    up to sleepAfter (20m), so a successful `wrangler deploy` alone does NOT
    mean the change is live.
    """
    out = {
        "status": "ok",
        "build": os.environ.get("BUILD_TAG", "dev"),
        "teams": len(load_local()),
        "cache_ttl": CACHE_TTL,
    }
    # Phase 3.5: burn summary + degraded-mode staleness flag, cheap enough for a
    # health probe. `degraded` is the flag the site/pages read to show that data
    # is stale because an upstream quota is exhausted (stale-but-honest, never
    # silently wrong).
    try:
        import budget  # noqa: PLC0415
        rep = budget.burn_report()
        out["budget"] = {s: {"pct": v["pct"], "level": v["level"],
                             "used": v["used"], "limit": v["limit"]}
                         for s, v in rep["sources"].items()}
        out["degraded"] = bool(rep["paused"])
        out["degraded_sources"] = rep["paused"]
    except Exception as e:  # noqa: BLE001 — never let metering break health
        out["budget_error"] = str(e)
    return out


@app.get("/api/budget")
def api_budget():
    """'What's our burn?' — the whole answer in ONE read (D1_CHECKLIST 3.5.4).

    No polling, no estimates: provider-reported usage where the provider tells us
    (CFBD / Odds API headers), our own confirmed counters otherwise.
    """
    import budget  # noqa: PLC0415
    rep = budget.burn_report()
    return {
        "as_of": rep["as_of"],
        "line": budget.burn_line(),
        "sources": rep["sources"],
        "alerts": rep["alerts"],
        "paused": rep["paused"],
        "alert_pct": budget.ALERT_PCT,
        "pause_pct": budget.PAUSE_PCT,
        "policy": "approaching a cap = tier-upgrade decision, not throttling (Jeff)",
    }

@app.get("/ping")
def ping():
    """Lightweight readiness probe for Cloudflare Containers."""
    return "ok"

def _enrich_with_composite(teams: list[dict]) -> list[dict]:
    """Attach composite + factor contributions to each team dict (in place).
    Single source of truth for the composite so rankings and analytics agree."""
    for team in teams:
        proj = project_score_multi_factor(team, is_home=True)
        team["composite"] = proj["composite"]
        team["sp_contribution"] = proj["sp_contribution"]
        team["fpi_contribution"] = proj["fpi_contribution"]
        team["srs_contribution"] = proj.get("srs_contribution", 0)
        team["cpi_contribution"] = proj["cpi_contribution"]
        team["elo_contribution"] = proj["elo_contribution"]
        team["rec_contribution"] = proj["rec_contribution"]
        team["epa_contribution"] = proj["epa_contribution"]
        team["efficiency_contribution"] = proj.get("efficiency_contribution", 0)
    return teams

@app.get("/api/analytics")
def api_analytics():
    """Get full CFBD analytics data for all teams (cached from disk, enriched with composite)."""
    cached = _cache_get(_analytics_cache, ANALYTICS_TTL)
    if cached:
        return cached
    teams = _load_cfbd_analytics_file()
    if not teams:
        teams = fetch_live_analytics()
    if not teams:
        raise HTTPException(502, "Analytics data unavailable")
    # Deep-copy to avoid mutating the source list (disk cache or fetch result)
    teams = [dict(t) for t in teams]
    _enrich_with_composite(teams)
    _overlay_records(teams)  # live W/L between analytics pulls
    result = {
        "week": datetime.now().strftime("%B %d, %Y"),
        "season": CFBD_YEAR,
        "updated": datetime.now().isoformat(),
        "teams": teams,
        "source": "cfbd",
        # Which season each fallback-capable rating input was actually served from.
        # A consumer (or a freshness gate) can now tell a current-season value from a
        # deliberate previous-season fallback instead of assuming recency.
        "input_vintages": dict(RATING_SOURCE_YEARS),
    }
    _cache_set(_analytics_cache, result)
    return result

# ── CFBD API Integration ──
# Keys come from environment (.env file or OS env) — never hardcode secrets in source.
CFBD_KEY = os.environ.get("CFBD_API_KEY", "")
PROPLINE_KEY = os.environ.get("PROPLINE_API_KEY", "")
THE_ODDS_API_KEY = os.environ.get("THE_ODDS_API_KEY", "")
if not CFBD_KEY or not PROPLINE_KEY:
    print("[!] WARNING: CFBD_API_KEY / PROPLINE_API_KEY not set in environment or .env")
    print("[!] Live CFBD/PropLine fetches will fail until you add them to .env")
CFBD_BASE = "https://api.collegefootballdata.com"
CFBD_HEADERS = {
    "Authorization": f"Bearer {CFBD_KEY}",
    "User-Agent": "cfb-analytics/1.0",
}
THE_ODDS_BASE = "https://api.the-odds-api.com/v4"
CFBD_YEAR = 2026  # 2026 season
CFBD_YEAR_FALLBACK = 2025

def _cfbd_get(endpoint: str, year: int = CFBD_YEAR, **extra) -> list:
    """Delegate to the SHARED CFBD client (cfbd_shared.cfbd_get) so the live app
    and the D1 backfill can never drift (CEO directive: no parallel impls).
    `extra` carries ranged params (week, seasonType, team, ...)."""
    return cfbd_shared.cfbd_get(endpoint, year=year, **extra)


class _PropLineQuotaExhausted(Exception):
    """Daily PropLine quota hit (429 with large Retry-After). Stop further calls."""


def _http_get_with_headers(url: str, params: dict | None = None, headers: dict | None = None,
                           retries: int = 3, base_delay: float = 1.0):
    """GET with exponential backoff retry. Returns (parsed JSON, response headers).

    429 handling honors Retry-After: small values (burst limit) sleep + retry;
    large values (daily cap — counts down to next UTC midnight) raise
    _PropLineQuotaExhausted so the caller can degrade instead of burning time.
    """
    last_err = None
    for attempt in range(retries):
        try:
            resp = _HTTP_CLIENT.get(url, headers=headers, params=params)
            if resp.status_code == 429:
                ra = int(resp.headers.get("Retry-After") or 0)
                if ra > 120:  # daily cap — retrying now is pointless
                    raise _PropLineQuotaExhausted(f"429 Retry-After={ra}s (daily quota exhausted)")
                last_err = f"429 burst limit (Retry-After {ra}s)"
            elif resp.status_code == 404:
                return None, {}
            else:
                resp.raise_for_status()
                return resp.json(), dict(resp.headers)
        except _PropLineQuotaExhausted:
            raise
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                # Burst-limit 429s: honor Retry-After (seconds until next token).
                delay = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
                m = re.search(r"Retry-After (\d+)s", str(last_err))
                if m:
                    delay = max(delay, int(m.group(1)))
                time.sleep(delay)
    print(f"[_http_get] {url} failed after {retries} retries: {last_err}")
    return None, {}


def _http_get(url: str, params: dict | None = None, headers: dict | None = None,
              retries: int = 3, base_delay: float = 1.0) -> list:
    """GET with exponential backoff retry. Returns parsed JSON, or [] on failure."""
    data, _ = _http_get_with_headers(url, params=params, headers=headers,
                                     retries=retries, base_delay=base_delay)
    return data

def _cfbd_teams() -> dict:
    """All teams keyed by school name (delegates to the shared matcher)."""
    return cfbd_shared.teams_by_name(CFBD_YEAR)

def _cfbd_fpi() -> dict:
    """Fetch FPI ratings, falling back to 2025 if 2026 empty."""
    data = _cfbd_get("ratings/fpi", CFBD_YEAR)
    if not data:
        data = _cfbd_get("ratings/fpi", CFBD_YEAR_FALLBACK)
    # Build rank from sorted list
    for i, t in enumerate(sorted(data, key=lambda x: x["fpi"], reverse=True)):
        t["ranking"] = i + 1
    return {t["team"]: t for t in data}

def _cfbd_sp() -> dict:
    """Fetch SP+ ratings, falling back to 2025 if 2026 empty."""
    data = _cfbd_get("ratings/sp", CFBD_YEAR)
    if not data:
        data = _cfbd_get("ratings/sp", CFBD_YEAR_FALLBACK)
    # SP+ uses 'team' key
    for i, t in enumerate(sorted(data, key=lambda x: x["rating"], reverse=True)):
        t["ranking"] = i + 1
    return {t["team"]: t for t in data}

def _cfbd_recruiting() -> dict:
    """Fetch team recruiting rankings, falling back to 2025 if 2026 empty."""
    data = _cfbd_get("recruiting/teams", CFBD_YEAR)
    if not data:
        data = _cfbd_get("recruiting/teams", CFBD_YEAR_FALLBACK)
    return {t["team"]: t for t in data}

# Which SEASON each fallback-capable input was actually served from.
#
# srs / elo / talent fall back to the PREVIOUS season when the current season's endpoint
# returns empty. Nothing recorded which happened, so a year-old value could be displayed
# as if it were current — SRS was doing exactly that while carrying 12% of the composite
# weight, and it was invisible on the analytics page. Consumers can now label the vintage
# instead of implying it, and a freshness gate can distinguish "stale" from "by design".
RATING_SOURCE_YEARS: dict = {}


def _cfbd_talent() -> dict:
    """Fetch 247Sports Team Talent Composite (85-man full roster talent), falling back to 2025."""
    data = _cfbd_get("talent", CFBD_YEAR)
    year = CFBD_YEAR
    if not data:
        data = _cfbd_get("talent", CFBD_YEAR_FALLBACK)
        year = CFBD_YEAR_FALLBACK
    RATING_SOURCE_YEARS["talent"] = year
    return {t["team"]: t for t in (data or [])}

def _cfbd_srs() -> dict:
    """Fetch SRS ratings, falling back to 2025 if 2026 empty."""
    data = _cfbd_get("ratings/srs", CFBD_YEAR)
    year = CFBD_YEAR
    if not data:
        data = _cfbd_get("ratings/srs", CFBD_YEAR_FALLBACK)
        year = CFBD_YEAR_FALLBACK
    RATING_SOURCE_YEARS["srs"] = year
    return {t["team"]: t for t in data}

def _cfbd_elo() -> dict:
    """Fetch Elo ratings, falling back to 2025 if 2026 empty."""
    data = _cfbd_get("ratings/elo", CFBD_YEAR)
    year = CFBD_YEAR
    if not data:
        data = _cfbd_get("ratings/elo", CFBD_YEAR_FALLBACK)
        year = CFBD_YEAR_FALLBACK
    RATING_SOURCE_YEARS["elo"] = year
    return {t["team"]: t for t in data}

def _cfbd_records(year: int = CFBD_YEAR) -> dict:
    """Fetch W/L records from CFBD /records, keyed by team name.

    Returns {team: {"wins": int, "losses": int, "games": int}} from the
    total (all games incl. neutral-site) bucket. No year fallback — records
    of 0-0 are correct pre-season, so a 2025 fallback would be WRONG data.
    Empty dict on any failure; callers must treat missing keys as unknown,
    not 0-0 (that's how the site showed stale 0-0s all season).
    """
    try:
        data = _cfbd_get("records", year)
        if not data:
            return {}
        out: dict[str, dict] = {}
        for r in data:
            # FBS only — the site never displays FCS/Division rows.
            if (r.get("classification") or "").lower() != "fbs":
                continue
            total = r.get("total") or {}
            out[r.get("team", "")] = {
                "wins": int(total.get("wins", 0) or 0),
                "losses": int(total.get("losses", 0) or 0),
                "games": int(total.get("games", 0) or 0),
            }
        return out
    except Exception as e:
        print(f"[records fetch failed] {e}")
        return {}

# Records freshness cache: records change after every game, far more often
# than the twice-weekly analytics pull. 1h TTL keeps /api/rankings current
# within an hour of a final without hammering CFBD.
_records_cache: dict = {}
RECORDS_TTL = 3600

def _live_records() -> dict:
    """CFBD records with a 1h cache. Empty dict when CFBD is down."""
    cached = _cache_get(_records_cache, RECORDS_TTL)
    if cached is not None:
        return cached
    rec = _cfbd_records()
    if rec:
        _cache_set(_records_cache, rec)
    return rec

def _overlay_records(teams: list[dict]) -> int:
    """Overlay live CFBD W/L records onto team dicts IN PLACE.

    Only overwrites when CFBD actually returned the team — a CFBD outage
    leaves existing values untouched instead of resetting everyone to 0-0.
    Returns the number of teams overlaid.
    """
    rec = _live_records()
    if not rec:
        return 0
    n = 0
    for t in teams:
        r = rec.get(t.get("name", ""))
        if r:
            t["wins"], t["losses"] = r["wins"], r["losses"]
            n += 1
    return n

def _cfbd_season_stats() -> dict:
    """Fetch REAL season stats (stats/season + games) keyed by team name.

    Combines the two CFBD endpoints so every metric is real data, not a
    synthetic estimate:
      - off_ppg / def_ppg: from actual game scores (games endpoint)
      - off_ypp / def_ypp: total yards / plays (stats/season)
      - off_3rd / def_3rd: third-down conversion rate (stats/season)
      - turnover_margin:  turnovers minus turnovers allowed, per game
    Falls back to the 2025 season when 2026 has no data yet.
    """
    def _fetch(year: int) -> dict | None:
        try:
            stats = _cfbd_get("stats/season", year)
            games = _http_get(
                f"{CFBD_BASE}/games",
                params={"year": year},
                headers=CFBD_HEADERS,
            )
        except Exception as e:
            print(f"[stats fetch failed {year}] {e}")
            return None

        # Aggregate real scoring from completed games
        pf: dict[str, float] = {}
        pa: dict[str, float] = {}
        ng: dict[str, int] = {}
        for g in games:
            if not g.get("completed") or g.get("homePoints") is None:
                continue
            h, a = g["homeTeam"], g["awayTeam"]
            hp, ap = g["homePoints"], g["awayPoints"]
            pf[h] = pf.get(h, 0) + hp; pa[h] = pa.get(h, 0) + ap; ng[h] = ng.get(h, 0) + 1
            pf[a] = pf.get(a, 0) + ap; pa[a] = pa.get(a, 0) + hp; ng[a] = ng.get(a, 0) + 1

        # Aggregate per-team season stat lines (one record per statName)
        team_stats: dict[str, dict[str, float]] = {}
        for rec in stats:
            team = rec.get("team", "")
            team_stats.setdefault(team, {})[rec["statName"]] = float(rec.get("statValue", 0))

        out: dict[str, dict] = {}
        for team, s in team_stats.items():
            games_played = max(1, int(s.get("games", 0)))
            plays = s.get("passAttempts", 0) + s.get("rushingAttempts", 0)
            opp_plays = s.get("passAttemptsOpponent", 0) + s.get("rushingAttemptsOpponent", 0)
            to = s.get("turnovers", 0) - s.get("turnoversOpponent", 0)
            # HAVOC (added Sep 6, user call): CFBD has no native havoc stat, but
            # all components are here. Naming convention verified against the
            # turnover identity (turnoversOpponent == interceptionsOpponent +
            # fumblesLostOpponent, 131/131) and cross-team sack totals:
            #   unmarked fields = OUR defense produced (sacks/TFL/interceptionsOpponent
            #   = INTs our D caught; fumblesLostOpponent = fumbles we recovered)
            #   *Opponent fields = allowed to our offense
            def_havoc = None
            if opp_plays:
                dh = (s.get("tacklesForLoss", 0) + s.get("sacks", 0)
                      + s.get("interceptionsOpponent", 0) + s.get("fumblesLostOpponent", 0)) / opp_plays
                def_havoc = round(dh, 3)
            havoc_allowed = None
            if plays:
                ha = (s.get("tacklesForLossOpponent", 0) + s.get("sacksOpponent", 0)
                      + s.get("interceptions", 0) + s.get("fumblesLost", 0)) / plays
                havoc_allowed = round(ha, 3)
            out[team] = {
                # Real points per game from game scores (fall back to stats-based if no games found)
                "off_ppg": round(pf.get(team, 0) / ng.get(team, 1), 1) if team in ng else None,
                "def_ppg": round(pa.get(team, 0) / ng.get(team, 1), 1) if team in ng else None,
                "off_ypp": round(s.get("totalYards", 0) / plays, 2) if plays else None,
                "def_ypp": round(s.get("totalYardsOpponent", 0) / opp_plays, 2) if opp_plays else None,
                "off_3rd": round(s.get("thirdDownConversions", 0) / max(1, s.get("thirdDowns", 1)), 3),
                "def_3rd": round(s.get("thirdDownConversionsOpponent", 0) / max(1, s.get("thirdDownsOpponent", 1)), 3),
                "turnover_margin": round(to / games_played, 2),
                "def_havoc": def_havoc,          # defensive havoc rate (TFL+sack+INT+fum / opp plays)
                "havoc_allowed": havoc_allowed,  # havoc our offense allows (same, mirrored)
            }
        # BUGFIX Sep 6: this return was missing entirely — _fetch always
        # returned None, so real season stats (PPG/YPP/3rd-downs/TO margin)
        # were silently empty all season and the model ran on neutral
        # defaults until real ratings accumulated.
        return out
    result = _fetch(CFBD_YEAR)
    if not result:
        result = _fetch(CFBD_YEAR_FALLBACK)
    return result or {}


def _cfbd_returning() -> dict:
    """Fetch experience-weighted returning production from CFBD /player/returning.
    Returns dict keyed by team name with returning EPA metrics:
      - returning_ppa: total PPA (EPA) returning from last year's players
      - pct_ppa_returning: fraction of team's EPA returning (0–1)
      - pct_pass_ppa: fraction of passing EPA returning
      - pct_rush_ppa: fraction of rushing EPA returning
    Falls back to 2025 if 2026 has no data yet.
    """
    data = _cfbd_get("player/returning", CFBD_YEAR)
    if not data:
        data = _cfbd_get("player/returning", CFBD_YEAR_FALLBACK)
    return {t["team"]: t for t in data}


def _cfbd_roster_experience() -> dict:
    """Fetch roster data and compute experience-weighted depth.
    For each team, returns:
      - roster_count: total rostered players
      - avg_year: average player year (1=freshman → 4=senior)
      - experience_score: normalized experience depth (0–100, higher = more experienced)
    Falls back to 2025 if 2026 has no data yet.
    """
    data = _cfbd_get("roster", CFBD_YEAR)
    if not data:
        data = _cfbd_get("roster", CFBD_YEAR_FALLBACK)
    if not data:
        return {}
    by_team: dict[str, list] = {}
    for p in data:
        by_team.setdefault(p.get("team", ""), []).append(p)
    out = {}
    for team, players in by_team.items():
        years = [p.get("year", 0) for p in players if p.get("year")]
        avg_year = round(sum(years) / len(years), 2) if years else 0
        # Experience score: weighted by position — offensive/defensive starters matter most
        # Simple approach: avg_year normalized on a 1-4 scale → 0-100
        exp_score = round((avg_year - 1) / 3 * 100, 1) if avg_year else 0
        out[team] = {
            "roster_count": len(players),
            "avg_year": avg_year,
            "experience_score": exp_score,
        }
    return out


def _cfbd_ppa() -> dict:
    """Fetch EPA/PPA (Expected Points Added) data from CFBD /ppa/teams endpoint.
    Returns dict keyed by team name with offense EPA metrics.
    Falls back to 2025 season if 2026 has no data yet.
    """
    data = _cfbd_get("ppa/teams", CFBD_YEAR)
    if not data:
        data = _cfbd_get("ppa/teams", CFBD_YEAR_FALLBACK)
    out = {}
    for rec in data:
        team = rec.get("team", "")
        off = rec.get("offense", {})
        defense = rec.get("defense", {})
        out[team] = {
            "epa_play": round(off.get("overall", 0), 3),
            "epa_pass": round(off.get("passing", 0), 3),
            "epa_rush": round(off.get("rushing", 0), 3),
            "def_epa_play": round(defense.get("overall", 0), 3),
            "def_epa_pass": round(defense.get("passing", 0), 3),
            "def_epa_rush": round(defense.get("rushing", 0), 3),
        }
    return out


def _cfbd_advanced_stats() -> dict:
    """Fetch advanced season stats (success rate, points per opportunity, line yards, stuff rate)
    from CFBD /stats/season/advanced.
    Single call returns all 138 FBS teams. Falls back to CFBD_YEAR_FALLBACK if empty.
    """
    data = _cfbd_get("stats/season/advanced", CFBD_YEAR)
    if not data:
        data = _cfbd_get("stats/season/advanced", CFBD_YEAR_FALLBACK)
    out = {}
    for d in (data or []):
        team = d.get("team", "")
        off = d.get("offense", {}) or {}
        defn = d.get("defense", {}) or {}
        out[team] = {
            "off_success_rate": round(off.get("successRate", 0), 4) if off.get("successRate") is not None else None,
            "def_success_rate": round(defn.get("successRate", 0), 4) if defn.get("successRate") is not None else None,
            "off_ppo": round(off.get("pointsPerOpportunity", 0), 2) if off.get("pointsPerOpportunity") is not None else None,
            "def_ppo": round(defn.get("pointsPerOpportunity", 0), 2) if defn.get("pointsPerOpportunity") is not None else None,
            "off_line_yards": round(off.get("lineYards", 0), 2) if off.get("lineYards") is not None else None,
            "def_line_yards": round(defn.get("lineYards", 0), 2) if defn.get("lineYards") is not None else None,
            "off_stuff_rate": round(off.get("stuffRate", 0), 3) if off.get("stuffRate") is not None else None,
            "def_stuff_rate": round(defn.get("stuffRate", 0), 3) if defn.get("stuffRate") is not None else None,
            "off_power_success": round(off.get("powerSuccess", 0), 3) if off.get("powerSuccess") is not None else None,
            "def_power_success": round(defn.get("powerSuccess", 0), 3) if defn.get("powerSuccess") is not None else None,
            "off_explosiveness": round(off.get("explosiveness", 0), 3) if off.get("explosiveness") is not None else None,
            "def_explosiveness": round(defn.get("explosiveness", 0), 3) if defn.get("explosiveness") is not None else None,
        }
    return out


def _cfbd_drives_for_teams(team_names: list[str]) -> dict:
    """Fetch drive-level data and compute possession-based metrics for both offense and defense.
    Returns dict keyed by team name with offensive AND defensive metrics:
      Offense: pts_per_poss, td_rate, fg_rate, turnover_rate
      Defense: def_pts_per_poss, def_td_rate, def_fg_rate, def_turnover_created
    Use CFBD_YEAR first, falls back to CFBD_YEAR_FALLBACK if no data.
    Fetches all drives in ONE call per year, then slices by team.
    """
    drives = _cfbd_get("drives", CFBD_YEAR)
    if not drives:
        drives = _cfbd_get("drives", CFBD_YEAR_FALLBACK)
    if not drives:
        return {}
    # Group drives by offense AND defense
    off_by_team: dict[str, list] = {}
    def_by_team: dict[str, list] = {}
    for d in drives:
        off = d.get("offense", "")
        def_ = d.get("defense", "")
        if off in team_names:
            off_by_team.setdefault(off, []).append(d)
        if def_ in team_names:
            def_by_team.setdefault(def_, []).append(d)
    out = {}
    for name in team_names:
        # Offense metrics (drives where this team is on offense)
        team_drives = off_by_team.get(name, [])
        if team_drives:
            total = len(team_drives)
            tds = sum(1 for d in team_drives if d.get("driveResult") == "TD")
            fgs = sum(1 for d in team_drives if d.get("driveResult") == "FG")
            tos = sum(1 for d in team_drives if d.get("driveResult") in ("INT", "FUMBLE"))
            scoring = sum(1 for d in team_drives if d.get("scoring") is True)
            pts = 0.0
            for d in team_drives:
                if d.get("scoring"):
                    pts += max(0, d.get("endOffenseScore", 0) - d.get("startOffenseScore", 0))
            out[name] = {
                "pts_per_poss": round(pts / max(1, total), 2),
                "td_rate": round(tds / total * 100, 1),
                "fg_rate": round(fgs / max(1, scoring) * 100, 1),
                "turnover_rate": round(tos / total * 100, 1),
            }
        # Defense metrics (drives where this team is on defense)
        opp_drives = def_by_team.get(name, [])
        if opp_drives:
            d_total = len(opp_drives)
            d_tds = sum(1 for d in opp_drives if d.get("driveResult") == "TD")
            d_fgs = sum(1 for d in opp_drives if d.get("driveResult") == "FG")
            d_tos = sum(1 for d in opp_drives if d.get("driveResult") in ("INT", "FUMBLE"))
            d_scoring = sum(1 for d in opp_drives if d.get("scoring") is True)
            d_pts = 0.0
            for d in opp_drives:
                if d.get("scoring"):
                    d_pts += max(0, d.get("endOffenseScore", 0) - d.get("startOffenseScore", 0))
            out.setdefault(name, {})
            out[name].update({
                "def_pts_per_poss": round(d_pts / max(1, d_total), 2),
                "def_td_rate": round(d_tds / d_total * 100, 1),
                "def_fg_rate": round(d_fgs / max(1, d_scoring) * 100, 1),
                "def_turnover_created": round(d_tos / d_total * 100, 1),
            })
    return out


def _budget_note(source: str, headers, calls: int = 1) -> None:
    """Meter one third-party call + capture the provider's quota headers (Phase 3.5).

    Never fatal: metering must not be able to break a data fetch.
    """
    try:
        import budget  # noqa: PLC0415
        budget.record(source, calls)
        if headers:
            budget.note_headers(source, dict(headers))
    except Exception:  # noqa: BLE001
        pass


def _the_odds_fetch() -> list[dict]:
    """Fetch live NCAAF spreads and totals from The Odds API.

    Uses the headers-returning GET so we can read x-requests-used/remaining —
    the provider's own accounting, which is authoritative and stateless (it
    survives a container recycle, unlike our counters).
    """
    url = f"{THE_ODDS_BASE}/sports/americanfootball_ncaaf/odds"
    params = {
        "apiKey": THE_ODDS_API_KEY,
        "regions": "us",
        "markets": "spreads,totals",
        "oddsFormat": "decimal",
    }
    # Degraded mode: exhausted quota => stop calling, serve cached lines (stale-but-honest)
    try:
        import budget  # noqa: PLC0415
        if not budget.should_call("odds"):
            print("[Odds] The Odds API paused by budget — serving cached lines")
            return []
    except Exception:  # noqa: BLE001
        pass
    data, hdrs = _http_get_with_headers(url, params=params, retries=3, base_delay=1.0)
    _budget_note("odds", hdrs)
    return data if isinstance(data, list) else (data.get("events", []) if data else [])
def _cfbd_lines() -> list:
    """Fetch betting lines from CFBD API."""
    try:
        data = _cfbd_get("lines", CFBD_YEAR)
        return data
    except Exception as e:
        print(f"[CFBD lines fetch failed] {e}")
        return []

def _cfbd_weather(week: int, year: int = CFBD_YEAR) -> dict:
    """Fetch game kickoff weather (wind, temp, condition, dome flag) from CFBD /games/weather."""
    try:
        url = f"{CFBD_BASE}/games/weather"
        params = {"year": year, "week": week}
        # CFBD_HEADERS is REQUIRED here: /games/weather is an authenticated
        # endpoint and returns 401 without it. Omitting the header made the
        # whole wind/temp overlay silently inert (every game weather={},
        # wind_penalty=0) while still looking "wired up" in the API payload.
        data = _http_get(url, params=params, headers=CFBD_HEADERS, retries=2, base_delay=0.5)
        if not data:
            data = _http_get(url, params={"year": CFBD_YEAR_FALLBACK, "week": week},
                             headers=CFBD_HEADERS, retries=2, base_delay=0.5)
        out = {}
        for g in (data or []):
            h = g.get("homeTeam", "")
            a = g.get("awayTeam", "")
            if h and a:
                wind = float(g.get("windSpeed") or 0.0)
                temp = float(g.get("temperature") or 70.0)
                indoor = bool(g.get("gameIndoors"))
                cond = g.get("weatherCondition") or ("Indoor" if indoor else "Clear")
                out[(h, a)] = {
                    "wind": round(wind, 1),
                    "temp": round(temp, 1),
                    "indoor": indoor,
                    "condition": cond,
                }
        return out
    except Exception as e:
        print(f"[CFBD weather fetch failed] {e}")
        return {}


# ── PropLine quota management (Hobby tier: 5,000 req/day, hard reset at UTC midnight) ──
# The Aug 29 incident: per-event /odds calls for every event (~113/cycle x hourly
# refreshes) exhausted the daily quota by ~6pm UTC on game day. Now: bulk /odds is
# ONE call and carries complete spreads+totals for every event (verified identical
# to per-event data), so per-event best-line calls are a bounded, quota-gated extra.
_PROPLINE_QUOTA = {"limit": None, "used": None, "remaining": None, "reset": None}
# Latch set when a 429 daily-cap is hit: skip best-line enrichment for the rest of
# the UTC day (bulk /odds still works — it's one call and we keep trying it).
_PROPLINE_QUOTA_DEAD = {"until_reset": 0}

def _propline_quota_update(headers: dict) -> None:
    """Track daily quota from X-Daily-* headers on every PropLine response."""
    try:
        # httpx header dicts are lowercase — normalize for case-insensitive lookup.
        h = {str(k).lower(): v for k, v in (headers or {}).items()}
        for hname, field in (("x-daily-limit", "limit"), ("x-daily-used", "used"),
                             ("x-daily-remaining", "remaining"), ("x-daily-reset", "reset")):
            v = h.get(hname)
            if v is not None and str(v).strip().isdigit():
                _PROPLINE_QUOTA[field] = int(str(v))
        # A fresh response with remaining>0 means the UTC-day reset happened.
        if _PROPLINE_QUOTA_DEAD["until_reset"] and (_PROPLINE_QUOTA.get("remaining") or 0) > 0:
            _PROPLINE_QUOTA_DEAD["until_reset"] = 0
    except Exception:
        pass

def _propline_quota_mark_dead() -> None:
    """Called on a daily-cap 429 — no more best-line calls until the quota resets."""
    reset = _PROPLINE_QUOTA.get("reset") or (time.time() + 86400)
    _PROPLINE_QUOTA_DEAD["until_reset"] = int(reset)

def _propline_quota_str() -> str:
    q = _PROPLINE_QUOTA
    if q["limit"] is None:
        return "unknown (no PropLine response yet)"
    pct = f"{q['used'] / q['limit'] * 100:.0f}%" if q.get("used") is not None else "?"
    reset = ""
    if q.get("reset"):
        try:
            reset = " resets " + datetime.fromtimestamp(q["reset"], timezone.utc).strftime("%H:%M UTC")
        except Exception:
            pass
    return (f"PropLine quota {q['used']}/{q['limit']} ({pct}) remaining={q['remaining']}"
            f"{reset}")

def _propline_best_line_allowed() -> bool:
    """Per-event best-line fetches only while >20% of the daily quota remains.

    Below that we degrade to bulk-only (1 call) so the site keeps working and
    the remaining budget can't be burned on enrichment. First cycle (no header
    seen yet) is allowed — the bulk response updates state before any extra calls.
    """
    q = _PROPLINE_QUOTA
    if _PROPLINE_QUOTA_DEAD["until_reset"] and time.time() < _PROPLINE_QUOTA_DEAD["until_reset"]:
        return False  # daily cap hit earlier today — bulk-only until UTC reset
    if q["limit"] in (None, 0):
        return True
    rem = q.get("remaining")
    if rem is None:
        rem = max(0, q["limit"] - (q.get("used") or 0))
    return rem > 0.2 * q["limit"]

# Books that are prediction markets / exchanges, not sportsbooks — never attribute
# a displayed line to them (Kalshi/Polymarket quotes aren't bettable at those prices).
_PROPLINE_NON_SPORTSBOOKS = {"kalshi", "polymarket", "smarkets", "novig", "prophetx"}

def _parse_best_line(payload: dict) -> dict | None:
    """Reduce a /best-line payload to consensus market lines with book attribution.

    For each market (spreads/totals), the MAIN line is the point quoted by the most
    books across all sides — that's the market consensus, not one book's alt ladder.
    Attribution picks the sharpest sportsbook quoting that exact point (same priority
    order as _build_odds_map). Returns {"spread": {...}, "total": {...}} or None.
    """
    if not payload or payload.get("redacted"):
        return None
    lines = {}
    for mk, out_key in (("spreads", "spread"), ("totals", "total")):
        entries = [ln for ln in payload.get("lines", []) if ln.get("market_key") == mk]
        if not entries:
            continue

        def _rows(ln):
            rows = []
            for sd in (ln.get("sides") or {}).values():
                rows += sd.get("all_prices") or []
            return rows

        # MAIN line = the point quoted by the most distinct SPORTSBOOKS. Exchange/
        # prediction-market rungs (a Kalshi ladder alone can have 40+ points) must
        # not count as consensus votes — they're one venue's depth, not agreement.
        def _score(ln):
            rows = [x for x in _rows(ln) if x.get("book") not in _PROPLINE_NON_SPORTSBOOKS]
            return (len({x.get("book") for x in rows}), len(rows))

        main = max(entries, key=_score)
        all_rows = _rows(main)
        sport_rows = [x for x in all_rows if x.get("book") not in _PROPLINE_NON_SPORTSBOOKS]
        pool = sport_rows or all_rows
        book = next((b for b in ("pinnacle", "draftkings", "fanduel", "betrivers", "bovada")
                     if any(x.get("book") == b for x in pool)), None)
        title = next((x.get("book_title") for x in pool if x.get("book") == book), None) or (book or "")
        entry = {"point": main.get("point"), "n_quoters": len(sport_rows) or len(all_rows),
                 "book": book, "book_title": title}
        if mk == "spreads":
            # SPREAD SIDE ATTRIBUTION (Sep 8 fix): a ladder rung's point is quoted
            # RELATIVE TO THE SIDE TEAM — "Missouri" at -27.5 means Missouri -27.5;
            # "Kansas" at +27.5 means Kansas +27.5. The old parser stored the raw
            # point with no team, so an away favorite's line (Oregon -22.5) was
            # read as HOME -22.5 and every ATS pick on an away-favorite game
            # flipped to the wrong side. Single-side rungs are unambiguous;
            # two-side rungs don't identify the owner, so null the point (the
            # caller falls back to the team-tagged bulk line, always correct).
            side_names = [k for k in (main.get("sides") or {}).keys()]
            if len(side_names) == 1:
                entry["side"] = side_names[0]
            else:
                entry["point"] = None
        lines[out_key] = entry
    return lines or None

# Betting window for per-event best-line enrichment: only games kicking off within
# this many days get the extra call (lines that far out are opening numbers anyway).
PROPLINE_BEST_LINE_WINDOW_DAYS = int(os.environ.get("PROPLINE_BEST_LINE_WINDOW_DAYS", "10"))
# Consensus guard threshold: a single-book line diverging from the multi-book
# consensus by more than this many points is treated as wrong (glitch or live
# artifact that slipped the kickoff filter) and replaced with the consensus.
CONSENSUS_GUARD_PTS = 6.0
# Tiered freshness inside the window — keeps game-day volume well under quota:
#   kickoff within 48h  -> best-line every refresh cycle (lines move fast now)
#   beyond 48h          -> at most once per day (opening lines barely move)
PROPLINE_NEAR_HOURS = int(os.environ.get("PROPLINE_NEAR_HOURS", "48"))
BEST_LINE_TS_FILE = BASE_DIR / "data" / "best_line_ts.json"
# Last-known best-line per event id — reused on cycles where a far-out game is not
# due for a fresh call, so every game keeps its consensus line (not just the ones
# fetched this cycle).
BEST_LINE_STORE_FILE = BASE_DIR / "data" / "best_line_store.json"

def _load_best_line_ts() -> dict:
    try:
        if BEST_LINE_TS_FILE.exists():
            return json.loads(BEST_LINE_TS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}

def _save_best_line_ts(ts_map: dict):
    try:
        # Keep only the last 3 days of entries so the file can't grow unbounded.
        cutoff = time.time() - 3 * 86400
        BEST_LINE_TS_FILE.write_text(
            json.dumps({k: v for k, v in ts_map.items() if v >= cutoff}), encoding="utf-8")
    except Exception as e:
        print(f"[Odds] best-line ts write failed: {e}")

def _load_best_line_store() -> dict:
    try:
        if BEST_LINE_STORE_FILE.exists():
            return json.loads(BEST_LINE_STORE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}

def _save_best_line_store(store: dict):
    try:
        cutoff = time.time() - 3 * 86400
        BEST_LINE_STORE_FILE.write_text(
            json.dumps({k: v for k, v in store.items() if v.get("_ts", 0) >= cutoff}),
            encoding="utf-8")
    except Exception as e:
        print(f"[Odds] best-line store write failed: {e}")

def _best_line_due(ev: dict, now, ts_map: dict) -> bool:
    """Should this event get a /best-line call THIS cycle?"""
    ct = ev.get("commence_time") or ""
    try:
        kick = datetime.fromisoformat(ct.replace("Z", "+00:00"))
    except Exception:
        return True  # unknown kickoff -> fetch rather than skip
    if kick <= now + timedelta(hours=PROPLINE_NEAR_HOURS):
        return True  # near-term game: every cycle
    last = ts_map.get(str(ev.get("id")), 0)
    return time.time() - last >= 86400  # far-out game: once per day

def _propline_event_needed(ev: dict, now) -> bool:
    """True if the event kicks off within the best-line betting window."""
    ct = ev.get("commence_time") or ""
    try:
        return datetime.fromisoformat(ct.replace("Z", "+00:00")) <= now + timedelta(days=PROPLINE_BEST_LINE_WINDOW_DAYS)
    except Exception:
        return True  # unknown kickoff -> keep the line rather than drop it

def _propline_fetch() -> list[dict]:
    """Fetch NCAAF spreads & totals from PropLine.

    Bulk /odds?markets=spreads,totals is ONE call and returns complete bookmaker
    data for every event (verified identical to per-event /odds — the old 100+
    per-event calls were pure quota burn). Per-event /best-line adds cross-book
    consensus lines + which book offers each; it runs only when:
      - >20% of the daily quota remains (X-Daily-Remaining), and
      - the game kicks off within PROPLINE_BEST_LINE_WINDOW_DAYS, and
      - tiered freshness: kickoff <= PROPLINE_NEAR_HOURS -> every cycle;
        further out -> at most once per day (last-known line reused meanwhile).
    Events carry "commence_time" so the kickoff filter in _fetch_odds_live works,
    and an optional "best_line" field consumed by _build_odds_map.
    """
    if not PROPLINE_KEY:  # Use module-level key (set from .env at startup)
        return []
    # Degraded mode: exhausted daily quota => stop calling, serve cached lines
    try:
        import budget  # noqa: PLC0415
        if not budget.should_call("propline"):
            print("[Odds] PropLine paused by budget — serving cached lines")
            return []
    except Exception:  # noqa: BLE001
        pass
    try:
        base = "https://api.prop-line.com/v1/sports/football_ncaaf"
        bulk, hdrs = _http_get_with_headers(
            f"{base}/odds", params={"apiKey": PROPLINE_KEY, "markets": "spreads,totals"},
            retries=3, base_delay=1.0)
        if not bulk:
            return []
        _propline_quota_update(hdrs)
        _budget_note("propline", hdrs)
        events = bulk if isinstance(bulk, list) else bulk.get("events", [])
        now = datetime.now(timezone.utc)
        ts_map = _load_best_line_ts()
        store = _load_best_line_store()
        # Reuse last-known best-line for games not due for a fresh call this cycle.
        for ev in events:
            eid = str(ev.get("id") or "")
            if eid and "best_line" not in ev and isinstance(store.get(eid), dict):
                ev["best_line"] = store[eid]
        window = [ev for ev in events if _propline_event_needed(ev, now)]
        due = [ev for ev in window if _best_line_due(ev, now, ts_map)]
        # Measure: how many per-event calls does the window+freshness cap save?
        print(f"[Odds] PropLine: {len(events)} events | window({PROPLINE_BEST_LINE_WINDOW_DAYS}d) "
              f"{len(window)} | due this cycle {len(due)}")
        if not _propline_best_line_allowed():
            print(f"[Odds] {_propline_quota_str()} — below 20%: bulk-only this cycle (no best-line)")
            return events
        # Per-event best-line, concurrency 4. (Comment historically read "memory-safe on
        # Render's 512MB box" — Render is decommissioned; the live container is Cloudflare
        # instance_type "basic" = 1 GiB, verified 2026-09-23. The cap itself is unchanged.)
        def _fetch_best_line(ev):
            eid = ev.get("id") or ev.get("event_id")
            if not eid:
                return None
            # A sibling worker may have hit the daily cap — stop before burning a call.
            if _PROPLINE_QUOTA_DEAD["until_reset"] and time.time() < _PROPLINE_QUOTA_DEAD["until_reset"]:
                return None
            try:
                bl, bh = _http_get_with_headers(
                    f"{base}/events/{eid}/best-line",
                    params={"apiKey": PROPLINE_KEY, "markets": "spreads,totals"},
                    retries=2, base_delay=0.5)
            except _PropLineQuotaExhausted:
                _propline_quota_mark_dead()  # stop enriching; bulk data still stands
                return None
            if bh:
                _propline_quota_update(bh)
                _budget_note("propline", bh)
            parsed = _parse_best_line(bl or {})
            if parsed is not None:
                ev["best_line"] = parsed
                store[str(eid)] = {**parsed, "_ts": time.time()}
                ts_map[str(eid)] = time.time()
            return None
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(_fetch_best_line, due))
        _save_best_line_ts(ts_map)
        _save_best_line_store(store)
        enriched = sum(1 for ev in events if "best_line" in ev)
        print(f"[Odds] PropLine best-line: {enriched}/{len(events)} events "
              f"(fetched {len(due)} this cycle | {_propline_quota_str()})")
        return events
    except _PropLineQuotaExhausted as e:
        print(f"[Odds] PropLine daily quota exhausted mid-fetch: {e} — bulk-only from now")
        return []
    except Exception as e:
        print(f"[PropLine fetch failed] {e}")
        return []



def _normalize_team_name(name: str) -> str:
    """Normalize bookmaker team names to our internal team names.
    Bookmakers use 'Location Mascot' format; we use 'Location' or 'School'.
    IMPORTANT: Check longer names first to avoid 'North Carolina' -> 'North'."""
    name_lower = name.lower()
    # Cached name map — built once on first call (80+ entries, rebuild per call is wasteful)
    if not _normalize_team_name._name_map:
        _normalize_team_name._name_map = [
        ("Alabama Crimson Tide", "Alabama"),
        ("Arkansas Razorbacks", "Arkansas"),
        ("Auburn Tigers", "Auburn"),
        ("BYU Cougars", "BYU"),
        ("Baylor Bears", "Baylor"),
        ("Boise State Broncos", "Boise State"),
        ("California Golden Bears", "California"),
        ("Cincinnati Bearcats", "Cincinnati"),
        ("Clemson Tigers", "Clemson"),
        ("Colorado Buffaloes", "Colorado"),
        ("Duke Blue Devils", "Duke"),
        ("East Carolina Pirates", "East Carolina"),
        ("Florida Atlantic Owls", "Florida Atlantic"),
        ("Florida Gators", "Florida"),
        ("Florida State Seminoles", "Florida State"),
        ("Fresno State Bulldogs", "Fresno State"),
        ("Georgia Bulldogs", "Georgia"),
        ("Georgia Tech Yellow Jackets", "Georgia Tech"),
        ("Houston Cougars", "Houston"),
        ("Illinois Fighting Illini", "Illinois"),
        ("Indiana Hoosiers", "Indiana"),
        ("Iowa Hawkeyes", "Iowa"),
        ("Iowa State Cyclones", "Iowa State"),
        ("Kansas Jayhawks", "Kansas"),
        ("Kansas State Wildcats", "Kansas State"),
        ("Kentucky Wildcats", "Kentucky"),
        ("Louisville Cardinals", "Louisville"),
        ("LSU Tigers", "LSU"),
        ("Marshall Thundering Herd", "Marshall"),
        ("Memphis Tigers", "Memphis"),
        ("Miami Hurricanes", "Miami"),
        ("Miami Florida Hurricanes", "Miami"),
        ("Miami Florida", "Miami"),
        ("Michigan State Spartans", "Michigan State"),
        ("Michigan Wolverines", "Michigan"),
        ("Minnesota Golden Gophers", "Minnesota"),
        ("Mississippi State Bulldogs", "Mississippi State"),
        ("Missouri Tigers", "Missouri"),
        ("NC State Wolfpack", "NC State"),
        ("NC St. Wolfpack", "NC State"),
        ("NC St.", "NC State"),
        ("Nebraska Cornhuskers", "Nebraska"),
        ("Nevada Wolf Pack", "Nevada"),
        ("New Mexico Lobos", "New Mexico"),
        ("North Carolina Tar Heels", "North Carolina"),
        ("North Texas Mean Green", "North Texas"),
        ("Northwestern Wildcats", "Northwestern"),
        ("Notre Dame Fighting Irish", "Notre Dame"),
        ("Ohio State Buckeyes", "Ohio State"),
        ("Oklahoma Sooners", "Oklahoma"),
        ("Oklahoma State Cowboys", "Oklahoma State"),
        ("Ole Miss Rebels", "Ole Miss"),
        ("Oregon Ducks", "Oregon"),
        ("Oregon State Beavers", "Oregon State"),
        ("Penn State Nittany Lions", "Penn State"),
        ("Pittsburgh Panthers", "Pittsburgh"),
        ("Purdue Boilermakers", "Purdue"),
        ("Rice Owls", "Rice"),
        ("Rutgers Scarlet Knights", "Rutgers"),
        ("Sam Houston State Bearkats", "Sam Houston"),
        ("San Diego State Aztecs", "San Diego State"),
        ("San Jose State Spartans", "San Jose State"),
        ("SMU Mustangs", "SMU"),
        ("South Carolina Gamecocks", "South Carolina"),
        ("South Florida Bulls", "South Florida"),
        ("Stanford Cardinal", "Stanford"),
        ("Syracuse Orange", "Syracuse"),
        ("TCU Horned Frogs", "TCU"),
        ("Temple Owls", "Temple"),
        ("Tennessee Volunteers", "Tennessee"),
        ("Texas A&M Aggies", "Texas A&M"),
        ("Texas Longhorns", "Texas"),
        ("Texas Tech Red Raiders", "Texas Tech"),
        ("Tulane Green Wave", "Tulane"),
        ("UAB Blazers", "UAB"),
        ("UCF Knights", "UCF"),
        ("UCLA Bruins", "UCLA"),
        ("UTSA Roadrunners", "UTSA"),
        ("Utah Utes", "Utah"),
        ("Utah State Aggies", "Utah State"),
        ("Vanderbilt Commodores", "Vanderbilt"),
        ("Virginia Cavaliers", "Virginia"),
        ("Virginia Tech Hokies", "Virginia Tech"),
        ("Wake Forest Demon Deacons", "Wake Forest"),
        ("Washington Huskies", "Washington"),
        ("Washington State Cougars", "Washington State"),
        ("West Virginia Mountaineers", "West Virginia"),
        ("Wisconsin Badgers", "Wisconsin"),
        ("Air Force Falcons", "Air Force"),
        ("Akron Zips", "Akron"),
        ("Appalachian State Mountaineers", "Appalachian State"),
        ("Arizona State Sun Devils", "Arizona State"),
        ("Arizona Wildcats", "Arizona"),
        ("Austin Peay Governors", "Austin Peay"),
        ("Ball State Cardinals", "Ball State"),
        ("Bethune-Cookman Wildcats", "Bethune-Cookman"),
        ("Bowling Green Falcons", "Bowling Green"),
        ("Buffalo Bulls", "Buffalo"),
        ("Central Michigan Chippewas", "Central Michigan"),
        ("Charlotte 49ers", "Charlotte"),
        ("Coastal Carolina Chanticleers", "Coastal Carolina"),
        ("Colorado State Rams", "Colorado State"),
        ("Delaware Blue Hens", "Delaware"),
        ("Duquesne Dukes", "Duquesne"),
        ("Eastern Michigan Eagles", "Eastern Michigan"),
        ("Florida International Panthers", "Florida International"),
        ("Fordham Rams", "Fordham"),
        ("Georgia Southern Eagles", "Georgia Southern"),
        ("Georgia State Panthers", "Georgia State"),
        ("Hawaii Rainbow Warriors", "Hawaii"),
        ("Idaho Vandals", "Idaho"),
        ("Indiana State Sycamores", "Indiana State"),
        ("Jacksonville State Gamecocks", "Jacksonville State"),
        ("James Madison Dukes", "James Madison"),
        ("Kennesaw State Owls", "Kennesaw State"),
        ("Kent State Golden Flashes", "Kent State"),
        ("Liberty Flames", "Liberty"),
        ("Louisiana Ragin Cajuns", "Louisiana"),
        ("Louisiana Tech Bulldogs", "Louisiana Tech"),
        ("Maine Black Bears", "Maine"),
        ("Middle Tennessee Blue Raiders", "Middle Tennessee"),
        ("Mississippi Valley State Delta Devils", "Mississippi Valley State"),
        ("Murray State Racers", "Murray State"),
        ("Navy Midshipmen", "Navy"),
        ("Nicholls State Colonels", "Nicholls"),
        ("New Hampshire Wildcats", "New Hampshire"),
        ("New Mexico State Aggies", "New Mexico State"),
        ("North Alabama Lions", "North Alabama"),
        ("North Carolina A&T Aggies", "North Carolina A&T"),
        ("North Dakota State Bison", "North Dakota State"),
        ("Northern Arizona Lumberjacks", "Northern Arizona"),
        ("Northern Illinois Huskies", "Northern Illinois"),
        ("Ohio Bobcats", "Ohio"),
        ("Old Dominion Monarchs", "Old Dominion"),
        ("Portland State Vikings", "Portland State"),
        ("Rhode Island Rams", "Rhode Island"),
        ("Sacramento State Hornets", "Sacramento State"),
        ("South Alabama Jaguars", "South Alabama"),
        ("South Dakota State Jackrabbs", "South Dakota State"),
        ("Southern Mississippi Golden Eagles", "Southern Miss"),
        ("Toledo Rockets", "Toledo"),
        ("Troy Trojans", "Troy"),
        ("Tulsa Golden Hurricane", "Tulsa"),
        ("UMass Minutemen", "UMass"),
        ("UNLV Rebels", "UNLV"),
        ("UTEP Miners", "UTEP"),
        ("UT Rio Grande Valley Vaqueros", "UTRGV"),
        ("Western Kentucky Hilltoppers", "Western Kentucky"),
        ("Western Michigan Broncos", "Western Michigan"),
        ("Youngstown St Penguins", "Youngstown State"),
        ("Abilene Christian Wildcats", "Abilene Christian"),
        ("Albany", "Albany"),
        ("Alcorn State Braves", "Alcorn State"),
        ("Arkansas Pine Bluff Golden Lions", "Arkansas-Pine Bluff"),
        ("Army Black Knights", "Army"),
        ("Bryant Bulldogs", "Bryant"),
        ("Charleston Southern Buccaneers", "Charleston Southern"),
        ("Citadel Bulldogs", "Citadel"),
        ("Eastern Illinois Panthers", "Eastern Illinois"),
        ("Eastern Kentucky Colonels", "Eastern Kentucky"),
        ("Furman Paladins", "Furman"),
        ("Houston Baptist Huskies", "Houston Baptist"),
        ("Idaho State Bengals", "Idaho State"),
        ("Lafayette Leopards", "Lafayette"),
        ("Lamar Cardinals", "Lamar"),
        ("LIU Sharks", "LIU"),
        ("Mercyhurst Lakers", "Mercyhurst"),
        ("Merrimack Warriors", "Merrimack"),
        ("Miami (OH) RedHawks", "Miami (OH)"),
        ("Missouri State Bears", "Missouri State"),
        ("Morgan State Bears", "Morgan State"),
        ("Norfolk State Spartans", "Norfolk State"),
        ("Northwestern State Demons", "Northwestern State"),
        ("Southeast Missouri State Redhawks", "SE Missouri State"),
        ("Southeastern Louisiana Lions", "Southeastern Louisiana"),
        ("Tarleton State Texans", "Tarleton State"),
        ("Towson Tigers", "Towson"),
        ("UL Monroe Warhawks", "UL Monroe"),
        ("UConn Huskies", "UConn"),
        ("Utah Tech Trailblazers", "Utah Tech"),
        ("VMI Keydets", "VMI"),
        ("Wyoming Cowboys", "Wyoming"),
        ("West Georgia Wolves", "West Georgia"),
    ]
    for pattern, school in _normalize_team_name._name_map:
        if pattern.lower() in name_lower:
            return school
    # Fallback: keep the FULL name when it has multiple words. Truncating to the
    # first word ("Utah State" -> "Utah") collides with a different team's game
    # (the actual Utah Utes) and corrupts the odds map. Single-word names pass
    # through unchanged; cross-feed matching for short-vs-full names is handled
    # by kickoff time in _find_odds_entry, not by name truncation.
    return name.strip() or name


_normalize_team_name._name_map = []  # populated lazily on first call to avoid per-call rebuild


def _build_odds_map(odds_data: list[dict]) -> dict:
    """Build a lookup dict keyed by (home_team, away_team) -> odds summary.

    Line source preference per game:
      1. best_line — cross-book consensus from PropLine /best-line (the point the
         most books quote), attributed to the sharpest sportsbook quoting it.
      2. priority book from bulk data (sharpest/most standard first): pinnacle,
         draftkings, fanduel, betrivers, bovada; falls back to the first book
         that offers a spread.
    """
    # Line source of record = Jeff's actual books (2026-09-23). betonlineag is his primary
    # sharp read and the book he can actually bet into, so the edge shown must be measured
    # against ITS line, not a book he cannot use. betmgm + williamhill_us follow.
    # NOTE: `williamhill_us` is Jeff's William Hill Nevada book. The app is William Hill
    # branded but prices off CAESARS lines, which is why the feed titles the key "Caesars".
    # Correct mapping, confirmed by Jeff 2026-09-23 - do not "fix" it to say William Hill.
    # pinnacle removed by request; it is also EU-region only in The Odds API.
    _BOOK_PRIORITY = ["betonlineag", "betmgm", "williamhill_us",
                      "draftkings", "fanduel", "betrivers", "bovada"]
    odds_map = {}
    for game in odds_data:
        home = _normalize_team_name(game.get("home_team", ""))
        away = _normalize_team_name(game.get("away_team", ""))
        key = (home, away)
        home_short = home.lower()
        away_short = away.lower()
        # 1) Cross-book consensus from /best-line (when fetched this cycle).
        bl = game.get("best_line") or {}
        bl_sp = bl.get("spread") if isinstance(bl.get("spread"), dict) else None
        bl_spread = bl_sp.get("point") if bl_sp else None
        bl_side = bl_sp.get("side") if bl_sp else None
        if bl_spread is not None:
            # SPREAD SIDE CONVERSION (Sep 8 fix): best-line rung points are quoted
            # relative to their SIDE team. Convert to home-relative
            # (negative = home favorite) so _ats_side/_grade_ats stay correct.
            if bl_side:
                bl_norm = _normalize_team_name(bl_side).lower()
                if bl_norm == away_short:
                    bl_spread = -bl_spread  # away-quoted -> home-relative
                elif bl_norm != home_short:
                    bl_spread = None  # unrecognized side: fall back to bulk
            else:
                bl_spread = None  # legacy store entry without side attribution
        bl_total = bl.get("total", {}).get("point") if isinstance(bl.get("total"), dict) else None
        # 2) Bulk per-book lines (fallback + fills any market best-line missed).
        # Collect the home-team spread + total from every book
        per_book = {}  # book_key -> {"title", "spread", "total"}
        for bm in game.get("bookmakers", []):
            bk = bm.get("key", "")
            title = bm.get("title", bk)
            spread = total = None
            for market in bm.get("markets", []):
                if market.get("key") == "spreads":
                    for outcome in market.get("outcomes", []):
                        oname = _normalize_team_name(outcome.get("name", ""))
                        onorm = oname.lower()
                        if onorm == home_short:
                            # Home spread (negative = home favorite). Take the first
                            # (primary) line; alt lines are later in the list.
                            if spread is None:
                                spread = outcome.get("point")
                        elif onorm == away_short and spread is None:
                            # Away spread as fallback (negate: away +7.5 => home -7.5)
                            p = outcome.get("point")
                            if p is not None:
                                spread = -p
                elif market.get("key") == "totals":
                    for outcome in market.get("outcomes", []):
                        if outcome.get("name") == "Over" and total is None:
                            total = outcome.get("point")
            if spread is not None or total is not None:
                per_book[bk] = {"title": title, "spread": spread, "total": total}
        # Pick the best book by priority, else the first available (bulk fallback)
        chosen = None
        for bk in _BOOK_PRIORITY:
            if bk in per_book:
                chosen = bk
                break
        if chosen is None and per_book:
            chosen = next(iter(per_book))
        b = per_book.get(chosen, {})
        # Merge: best-line consensus wins per market; bulk priority book fills gaps.
        spread = bl_spread if bl_spread is not None else b.get("spread")
        # FAVORITE-SIGN SANITY GUARD (Sep 8): the team-tagged bulk line is always
        # correct, so a negative (home-favorite) spread is only plausible when a
        # home-named line exists. A NEGATIVE spread with NO home-side quote
        # anywhere means the line was quoted against the AWAY favorite and the
        # side attribution got lost — drop it rather than ship a flipped ATS pick.
        if spread is not None and spread < 0:
            has_home_line = any(
                _normalize_team_name(o.get("name", "")).lower() == home_short
                for bm in game.get("bookmakers", [])
                for mk2 in bm.get("markets", []) if mk2.get("key") == "spreads"
                for o in mk2.get("outcomes", []))
            if not has_home_line:
                print(f"[Odds] {home}|{away}: negative spread {spread} with no "
                      f"home-side line — dropping (away-favorite misattribution)")
                spread = None
        total = bl_total if bl_total is not None else b.get("total")
        # Book attribution follows the line actually used (best-line's attributed
        # sportsbook when that market came from best-line, priority book otherwise).
        def _attr(mk):
            src = bl.get(mk)
            if isinstance(src, dict) and src.get("point") is not None:
                return src.get("book"), (src.get("book_title") or src.get("book"))
            return chosen, b.get("title", chosen)
        sp_book, sp_title = _attr("spread")
        tt_book, tt_title = _attr("total")
        # Top-level book = the one behind the spread (primary displayed line).
        # Per-market kind: "best_line" = cross-book consensus, "priority_book" =
        # single book from bulk data — the consensus guard in _fetch_odds_live
        # only overrides priority_book lines.
        odds_map[key] = {
            "spread": round(spread, 1) if spread is not None else None,
            "total": round(total, 1) if total is not None else None,
            "spread_home_favorite": spread is not None and spread < 0,
            "book": sp_book or tt_book,
            "book_title": sp_title or tt_title,
            "spread_kind": "best_line" if bl_spread is not None else "priority_book",
            "total_kind": "best_line" if bl_total is not None else "priority_book",
        }
    return odds_map


def _fetch_odds_map() -> dict:
    """Build a merged odds map from all sources, PropLine primary.

    Precedence:
      1. PropLine (Bovada lines) — primary, covers FCS/blowout games The Odds API misses
      2. The Odds API (FanDuel/DraftKings) — backup for mainstream matchups
      3. CFBD lines — last-resort fallback
    Games covered by a higher-priority source are NOT overwritten.

    Cache strategy (live API pulled ONCE per day):
      1. In-memory cache (ODDS_TTL)
      2. Disk cache (data/odds_cache.json, same TTL) — survives restarts
      3. Live fetch from all sources, then persist to memory + disk
    """
    cached = _cache_get(_odds_cache, ODDS_TTL)
    if cached:
        return cached
    disk = _odds_disk_cache_get()
    if disk:
        _cache_set(_odds_cache, disk)
        return disk
    # 3) Live fetch from all sources, then persist to memory + disk.
    # If the live fetch comes back empty (all sources failed), fall back to the
    # STALE disk cache so the site never goes dark — with a loud warning.
    odds_map = _fetch_odds_live()
    if not odds_map:
        stale = _odds_disk_cache_any_age()
        if stale:
            print("[Odds] live fetch empty — serving STALE disk cache as fallback")
            _cache_set(_odds_cache, stale)
            return stale
    return odds_map


def _odds_disk_cache_any_age() -> dict | None:
    """Load disk odds cache regardless of age (emergency stale fallback)."""
    try:
        if not ODDS_CACHE_FILE.exists():
            return None
        payload = json.loads(ODDS_CACHE_FILE.read_text(encoding="utf-8"))
        raw = payload.get("odds", {})
        return {tuple(k.split("|")): v for k, v in raw.items()} or None
    except Exception as e:
        print(f"[Odds] stale disk cache read failed: {e}")
        return None


def _find_odds_entry(odds_map: dict, home: str, away: str, kick_iso: str = ""):
    """Find an odds entry for (home, away), tolerating feed name differences.

    Exact key first; then a UNIQUE prefix match — PropLine sends short names
    ("Utah State") while The Odds API sends full names ("Utah State Aggies"),
    which normalize to different keys and would otherwise create duplicate
    entries for the same game (and false consensus-guard conflicts). A prefix
    candidate only counts when kickoff times agree within 30 min, so "Utah"
    never merges with a "Utah State" game at a different time.
    """
    key = (home, away)
    if key in odds_map:
        return key
    cands = [k for k in odds_map
             if (k[0].startswith(home) or home.startswith(k[0]))
             and (k[1].startswith(away) or away.startswith(k[1]))]
    if not cands:
        return None
    # When kickoff is known, require it to agree with the candidate — a unique
    # prefix match alone can still be the WRONG game ("Utah" vs "Utah State").
    kick = None
    if kick_iso:
        try:
            kick = datetime.fromisoformat(str(kick_iso).replace("Z", "+00:00"))
        except Exception:
            kick = None
    if kick is not None:
        timed = []
        for k in cands:
            kt = _ODDS_KICKOFF.get(k)
            if kt and abs((kt - kick).total_seconds()) <= 1800:
                timed.append(k)
        return timed[0] if len(timed) == 1 else None
    # No kickoff info — only trust an unambiguous prefix match.
    return cands[0] if len(cands) == 1 else None


# (home, away) -> kickoff datetime — populated during _fetch_odds_live so the
# prefix matcher can disambiguate same-prefix teams (Utah vs Utah State).
_ODDS_KICKOFF = {}

def _fetch_odds_live() -> dict:
    """Fetch odds from all sources (no cache reads). Non-empty on success."""
    odds_map = {}
    # 1) PropLine (primary — Bovada's feed) — PRE-GAME ONLY.
    # Bovada's API serves in-game adjusted lines for live events; those must
    # never enter the odds map (they polluted picks with live spreads, e.g.
    # UVA -8 in-game vs -3.5 consensus). Events are filtered by kickoff time.
    try:
        pl = _propline_fetch()
        pregame, live_dropped = [], 0
        now = datetime.now(timezone.utc)
        for ev in pl:
            ct = ev.get("commence_time") or ev.get("start_time") or ""
            try:
                started = bool(ct) and datetime.fromisoformat(
                    ct.replace("Z", "+00:00")) <= now
            except Exception:
                started = False  # unknown kickoff -> treat as pre-game
            if started:
                live_dropped += 1
                continue
            pregame.append(ev)
        if live_dropped:
            print(f"[Odds] PropLine: dropped {live_dropped} in-game events (live lines)")
        pl_map = _build_odds_map(pregame)
        for k, v in pl_map.items():
            v["source"] = "propline"
            odds_map[k] = v
        # Kickoff times per key — lets the prefix matcher disambiguate same-prefix
        # teams (Utah vs Utah State) when merging other feeds.
        for ev in pregame:
            k2 = (_normalize_team_name(ev.get("home_team", "")),
                  _normalize_team_name(ev.get("away_team", "")))
            ct = ev.get("commence_time") or ""
            try:
                _ODDS_KICKOFF[k2] = datetime.fromisoformat(ct.replace("Z", "+00:00"))
            except Exception:
                pass
    except Exception as e:
        print(f"[Odds] PropLine primary failed: {e}")
    # 2) The Odds API (multi-book consensus — fills gaps AND sanity-checks PropLine)
    # Same kickoff filter as PropLine: live-adjusted lines must never enter.
    try:
        toa_raw = _the_odds_fetch()
        now = datetime.now(timezone.utc)
        toa_pregame = []
        for ev in toa_raw:
            ct = ev.get("commence_time") or ""
            try:
                started = bool(ct) and datetime.fromisoformat(
                    ct.replace("Z", "+00:00")) <= now
            except Exception:
                started = False
            if not started:
                toa_pregame.append(ev)
        toa_map = _build_odds_map(toa_pregame)
        for k, v in toa_map.items():
            # Tolerant match: PropLine's short names ("Utah") vs TOA's full names
            # ("Utah Utes" -> "Utah") can produce different keys for the same game.
            found = _find_odds_entry(odds_map, k[0], k[1],
                                     next((e.get("commence_time", "") for e in toa_pregame
                                           if (_normalize_team_name(e.get("home_team", "")),
                                               _normalize_team_name(e.get("away_team", ""))) == k), ""))
            if found is None:
                v["source"] = "the_odds_api"
                odds_map[k] = v
            elif found != k:
                # Same game, different key (feed name mismatch) — merge TOA data
                # into the PropLine entry so downstream code sees one record.
                print(f"[Odds] merged duplicate keys {k[0]}|{k[1]} -> {found[0]}|{found[1]}")
                pl_v = odds_map[found]
                for mkt in ("spread", "total"):
                    if pl_v.get(mkt) is None and v.get(mkt) is not None:
                        pl_v[mkt] = v[mkt]
                # Consensus guard on the merged entry (same logic as exact-key case).
                for mkt, pl_kind in (("spread", pl_v.get("spread_kind")), ("total", pl_v.get("total_kind"))):
                    if pl_kind == "best_line":
                        continue  # already cross-book consensus — never override
                    pl_val = pl_v.get(mkt)
                    toa_val = v.get(mkt)
                    if (pl_val is not None and toa_val is not None
                            and abs(pl_val - toa_val) > CONSENSUS_GUARD_PTS):
                        print(f"[Odds] {found[0]}|{found[1]} {mkt}: PropLine {pl_val} vs "
                              f"consensus {toa_val} — using consensus")
                        pl_v[mkt] = toa_val
                        pl_v["book"] = v.get("book")
                        pl_v["book_title"] = v.get("book_title")
                        pl_v["source"] = "the_odds_api"
            else:
                # Consensus guard: PropLine SINGLE-BOOK line vs multi-book line.
                # A gap > 6 pts means one feed is wrong (glitch or live artifact
                # that slipped the kickoff filter) — trust the multi-book line.
                # Lines already sourced from /best-line are cross-book consensus,
                # so they're never overridden here.
                for mkt in ("spread", "total"):
                    pl_v, toa_v = odds_map[k].get(mkt), v.get(mkt)
                    if (pl_v is not None and toa_v is not None and abs(pl_v - toa_v) > CONSENSUS_GUARD_PTS
                            and odds_map[k].get(f"{mkt}_kind") != "best_line"):
                        print(f"[Odds] {k[0]}|{k[1]} {mkt}: PropLine {pl_v} vs "
                              f"consensus {toa_v} — using consensus")
                        odds_map[k][mkt] = toa_v
                        odds_map[k]["book"] = v.get("book")
                        odds_map[k]["book_title"] = v.get("book_title")
                        odds_map[k]["source"] = "the_odds_api"
    except Exception as e:
        print(f"[Odds] The Odds API backup failed: {e}")
    # 3) CFBD lines (last resort) — tolerant key match too, so a name mismatch
    # doesn't create a duplicate entry for a game we already have.
    try:
        for game in _cfbd_lines():
            hk = _normalize_team_name(game.get("homeTeam", ""))
            ak = _normalize_team_name(game.get("awayTeam", ""))
            key = (hk, ak)
            if _find_odds_entry(odds_map, hk, ak) is None and game.get("lines"):
                line = game["lines"][0]
                odds_map[key] = {
                    "spread": round(line.get("spread"), 1) if line.get("spread") is not None else None,
                    "total": round(line.get("overUnder"), 1) if line.get("overUnder") is not None else None,
                    "spread_home_favorite": line.get("spread") is not None and line.get("spread") < 0,
                    "source": "cfbd",
                }
    except Exception as e:
        print(f"[Odds] CFBD lines fallback failed: {e}")
    # Only persist NON-EMPTY results. If every source failed (transient API
    # outage), keep the previous cache rather than poisoning it with {}.
    if odds_map:
        _cache_set(_odds_cache, odds_map)
        _odds_disk_cache_set(odds_map)
    else:
        print("[Odds] all sources failed — keeping previous cache")
    return odds_map


# ── Composite configuration — SINGLE SOURCE OF TRUTH ─────────────────────────
# Weights live here rather than inline in the formula because:
#   (a) there is exactly ONE place to change them, and
#   (b) they can be HASHED into model_version. Risk register D3 requires an
#       auto-hash of the composite config on every rankings_daily /
#       model_predictions row, so a mid-season weight change produces a VISIBLE
#       version break instead of silently mixing incomparable rows in the archive.
#
# The active weights MUST sum to 1.0 (asserted below) — the projected-score
# calibration assumes a 0-100 composite.
#
# fcs_rating / massey: SLOTS EXIST AND ARE DELIBERATELY INERT (weight 0.0).
# Enabling either changes what the model says, so per CFO doctrine and the
# checklist's own instruction it needs JEFF'S APPROVAL on the weights — the
# proposal is in D1_COMPOSITE_PROPOSAL.md. Do not move these without it.
COMPOSITE_CONFIG_DEFAULT = {
    "sp_plus": 0.18,       # anchors (45%)
    "fpi": 0.15,
    "srs": 0.12,
    "elo": 0.08,           # priors / trajectory (18%)
    "talent": 0.10,
    "efficiency": 0.37,    # real on-field efficiency
    "fcs_rating": 0.00,    # NOT ENABLED — awaits Jeff
    "massey": 0.00,        # NOT ENABLED — awaits Jeff
}
_ENABLED_KEYS = ("sp_plus", "fpi", "srs", "elo", "talent", "efficiency")


def composite_config() -> dict:
    """Active composite weights.

    Overridable by env (`COMPOSITE_WEIGHTS_JSON`) so a candidate weighting can be
    measured without a code change — the hash in composite_version() then changes
    automatically, which is what makes an experiment auditable.
    """
    cfg = dict(COMPOSITE_CONFIG_DEFAULT)
    raw = os.environ.get("COMPOSITE_WEIGHTS_JSON")
    if raw:
        try:
            for k, v in json.loads(raw).items():
                cfg[str(k)] = float(v)
        except Exception as e:  # noqa: BLE001
            print(f"[composite] ignoring bad COMPOSITE_WEIGHTS_JSON: {e}")
    return cfg


def composite_version() -> str:
    """Short stable hash of the ACTIVE composite config (risk register D3).

    Stored as `model_version` on rankings_daily and model_predictions rows, so
    rows produced under different weightings can never be silently compared.
    """
    cfg = composite_config()
    blob = json.dumps({k: round(float(v), 6) for k, v in sorted(cfg.items())},
                      sort_keys=True, separators=(",", ":"))
    return "c" + hashlib.sha256(blob.encode()).hexdigest()[:10]


def _assert_weights_sum(cfg: dict | None = None) -> None:
    cfg = cfg or composite_config()
    total = sum(float(cfg.get(k, 0.0)) for k in _ENABLED_KEYS)
    if abs(total - 1.0) > 1e-6:
        print(f"[composite] WARNING: enabled weights sum to {total:.4f}, expected 1.0 "
              f"— projections will be miscalibrated")


# ── FCS composite (RATING-DERIVED) ───────────────────────────────────────────
# ⚠ FLAGGED FOR FURTHER REVIEW / FINE-TUNING (Jeff, Sep 22 2026).
#
# Replaces a flat `fcs_composite = 16.0` that made every FCS opponent identical.
# These values are FITTED, not chosen. The margin formula is
#     margin = (comp_home - comp_away) + 2.5*HFA + boost
# so the composite gap maps 1:1 onto points and the FCS composite is identifiable
# directly as `comp_fbs + HFA - actual_margin`. Fitted over 103 completed 2026
# FBS-vs-FCS games (the only season with real FBS composites):
#
#     unranked FCS opponent    -> 15   (implied 14.8)
#     ranked FCS (poll top 25) -> 33   (implied 32.7)
#     no rating available      -> 19   (implied 19.0 overall)
#
# Mean |error| on those games: 21.5 pts (old flat 16 + 15 boost) -> 13.6 pts.
#
# KNOWN LIMITS — not settled, revisit before trusting in larger size:
#   * ~13.6 pts of error remains. One number per group is a coarse model.
#   * The rank curve is NOT monotone in the data (rank 1-5 implies 34.8, rank
#     6-15 implies 36.7; n=5 / n=10). Most likely because the top FCS teams
#     schedule STRONGER FBS opponents rather than being weaker. Only the 2-level
#     split is supported; finer rank resolution is not yet justified by data.
#   * Massey's full 1..128 ordering IS loaded (see _fcs_rank_for) and is the
#     input for that finer tuning.
FCS_COMPOSITE_UNRANKED = 15.0
FCS_COMPOSITE_RANKED = 33.0
FCS_COMPOSITE_FALLBACK = 19.0
FCS_RANKED_CUTOFF = 25        # Massey rank at/below which a team counts as ranked

_FCS_RANKS: dict[int, int] = {}      # CFBD team_id -> Massey FCS rank
_FCS_RANKS_TS = 0.0
FCS_RATINGS_TTL = 6 * 3600


def _team_id_for(name: str) -> int | None:
    """CFBD team id for a name, via the SAME alias-aware map the site uses."""
    try:
        aliases = cfbd_shared.team_aliases(CFBD_YEAR) or {}
    except Exception:  # noqa: BLE001
        return None
    hit = aliases.get(name)
    if hit:
        try:
            return int(hit)
        except (TypeError, ValueError):
            pass
    try:
        import massey_fcs
        idx = {}
        for k, v in aliases.items():
            try:
                idx.setdefault(massey_fcs._norm(k), int(v))
            except (TypeError, ValueError):
                continue
        for var in massey_fcs._variants(massey_fcs._norm(name)):
            if var in idx:
                return idx[var]
    except Exception:  # noqa: BLE001
        return None
    return None


def refresh_fcs_ratings(force: bool = False) -> int:
    """Load Massey's FCS board into memory, keyed by CFBD TEAM ID.

    Id-keyed on purpose: the app looks teams up by CFBD name, and Massey names
    diverge ("LIU Post" is CFBD "Long Island University"), so a name-keyed cache
    silently misses those teams. Joining through the shared alias-aware matcher
    resolves every divergence, not just the ones with a hand alias.

    NEVER raises: the site must not break if Massey is unreachable — it falls back
    to the fitted value, which is what the old flat constant effectively was.
    """
    global _FCS_RANKS, _FCS_RANKS_TS
    if not force and (time.time() - _FCS_RANKS_TS) < FCS_RATINGS_TTL:
        return len(_FCS_RANKS)
    try:
        import massey_fcs
        idx = massey_fcs.build_matcher(CFBD_YEAR)
        aliases = massey_fcs._alias_map()
        by_id: dict[int, int] = {}
        for r in massey_fcs.derive_ranks(massey_fcs.fetch_fcs()):
            tid = massey_fcs.match_team(r["massey_team"], idx, aliases)
            if tid:
                by_id[int(tid)] = int(r["fcs_rank"])
        if by_id:
            _FCS_RANKS = by_id
            _FCS_RANKS_TS = time.time()
    except Exception as e:  # noqa: BLE001
        print(f"[fcs] Massey ratings refresh failed (keeping {len(_FCS_RANKS)} "
              f"cached): {e}")
    return len(_FCS_RANKS)


def _fcs_rank_for(name: str) -> int | None:
    """Massey FCS rank for a CFBD team name, or None when unknown."""
    tid = _team_id_for(name)
    return _FCS_RANKS.get(tid) if tid is not None else None


def fcs_composite_for(name: str) -> float:
    """Composite to use for an FCS opponent: ranked, unranked, or fallback."""
    rank = _fcs_rank_for(name)
    if rank is None:
        return FCS_COMPOSITE_FALLBACK
    return FCS_COMPOSITE_RANKED if rank <= FCS_RANKED_CUTOFF else FCS_COMPOSITE_UNRANKED


def project_score_multi_factor(team_data: dict, is_home: bool = True, opp_composite: float | None = None) -> dict:
    """
    Multi-factor projected score model.
    Anchor Ratings: SP+ 18%, FPI 15%, SRS 12% (replaces collinear CPI)
    Program Priors & Outcomes: Elo 8%, Talent Prior (Recruiting + Returning) 10%
    Advanced On-Field Efficiency: 37%
      - Net Success Rate: 10% (down-to-down consistency)
      - Net EPA/PPA per Play: 9% (per-play points added)
      - Finishing Drives / PPO: 6% (scoring inside the 40)
      - Trench Dominance: 6% (Line Yards & Stuff Rate differential)
      - True Points Per Possession (PPD): 6%
    Plus turnover-regressed scoring tilt and home-field advantage.
    Returns projected score, win probability, and composite rating.
    """
    # Extract ratings with defaults from actual cached data
    # IMPORTANT: use `or default` semantics — stored 0/None means "no data"
    # and must fall back to a neutral value, not be treated as a real rating.
    # MISSING-DATA PRIOR (added Aug 30): teams with NO ratings (FCS, D2, new programs)
    # must not default to "league average" — that gave an FCS squad 24 pts vs Minnesota.
    classification = (team_data.get("classification") or "").upper()
    has_ratings = bool(team_data.get("sp_plus") or team_data.get("elo"))
    if not has_ratings and classification == "FCS":
        # NOT a flat constant any more. The composite comes from the FCS rating
        # source, falling back to the fitted average when nothing is known about
        # this opponent. See FCS_COMPOSITE_* for the fit and its provenance.
        fcs_composite = float(team_data.get("fcs_composite") or FCS_COMPOSITE_FALLBACK)
        base_score = max(6.0, 27.0 + (fcs_composite - 50.0) * 0.55)
        home_adj = 2.5 if is_home else -1.5
        projected_score = round(max(0.0, base_score + home_adj), 1)
        win_prob = round((1 / (1 + (2.718 ** (-0.08 * (fcs_composite - 50))))) * 100, 1)
        return {
            "projected_score": projected_score,
            "composite": fcs_composite,
            "win_probability": win_prob,
            "data_flag": "fcs_no_data",
        }
    sp_plus = team_data.get("sp_plus") or 0.0       # CFBD real scale ~ -40..+40, 0 = average
    fpi_wp = team_data.get("fpi_win_prob") or 50.0  # 0-100 scale
    srs_score = team_data.get("srs") or 0.0         # Sports Reference SRS (~ -25..+25, 0 = avg)
    rec_rank = team_data.get("recruiting_rank") or 80
    elo = team_data.get("elo") or 1500              # real Elo, ~1500 = average
    # Real season stats may be None when CFBD has no data (e.g. pre-season);
    # fall back to neutral league-average values only in that case.
    off_ppg = team_data.get("off_ppg") or 28.0    # points scored per game
    def_ppg = team_data.get("def_ppg") or 24.0    # points allowed per game

    # Normalize anchor models to a 0-100 scale
    sp_norm = max(0, min(100, (sp_plus + 40) / 80 * 100))
    fpi_norm = max(0, min(100, fpi_wp))
    srs_norm = max(0, min(100, (srs_score + 25) / 50 * 100))
    elo_norm = max(0, min(100, (elo - 1500) / 300 * 100 + 50))

    # Talent Prior (247Sports 85-man Team Talent Composite + Recruiting Class + Returning Production)
    talent_score = team_data.get("talent_score")
    rec_norm = max(0, min(100, (130 - rec_rank) / 129 * 100))
    if talent_score:
        talent_comp_norm = max(0, min(100, (talent_score - 250) / 750 * 100))
        program_talent = talent_comp_norm * 0.70 + rec_norm * 0.30
    else:
        program_talent = rec_norm
    pct_ret = team_data.get("pct_ppa_returning")
    ret_norm = max(0, min(100, (pct_ret if pct_ret is not None else 50.0)))
    talent_norm = program_talent * 0.6 + ret_norm * 0.4

    # Advanced On-Field Efficiency Metrics (37% combined)
    # 1. Net Success Rate (Offense SR minus Defense SR allowed; center 0 -> 50)
    off_sr = team_data.get("off_success_rate")
    def_sr = team_data.get("def_success_rate")
    if off_sr is not None and def_sr is not None:
        sr_norm = max(0, min(100, (off_sr - def_sr + 0.20) / 0.40 * 100))
    else:
        sr_norm = 50.0

    # 2. Net EPA / PPA per play (center 0 -> 50)
    epa = team_data.get("epa_play") or 0.0
    def_epa = team_data.get("def_epa_play") or 0.0
    epa_norm = max(0, min(100, (epa - def_epa + 0.40) / 0.80 * 100))

    # 3. Finishing Drives (Points per Opportunity / inside opp 40; center 0 -> 50)
    off_ppo = team_data.get("off_ppo")
    def_ppo = team_data.get("def_ppo")
    if off_ppo is not None and def_ppo is not None:
        ppo_norm = max(0, min(100, (off_ppo - def_ppo + 2.5) / 5.0 * 100))
    else:
        ppo_norm = 50.0

    # 4. Trench Dominance (Line Yards + Stuff Rate differential)
    off_ly = team_data.get("off_line_yards")
    def_ly = team_data.get("def_line_yards")
    off_st = team_data.get("off_stuff_rate")
    def_st = team_data.get("def_stuff_rate")
    if all(v is not None for v in (off_ly, def_ly, off_st, def_st)):
        ly_norm = max(0, min(100, (off_ly - def_ly + 1.5) / 3.0 * 100))
        st_norm = max(0, min(100, (def_st - off_st + 0.15) / 0.30 * 100))
        trench_norm = ly_norm * 0.6 + st_norm * 0.4
    else:
        trench_norm = 50.0

    # 5. True Points Per Possession (PPD)
    pts_poss = team_data.get("pts_per_poss")
    def_pts_poss = team_data.get("def_pts_per_poss")
    if pts_poss is not None and def_pts_poss is not None:
        ppd_norm = max(0, min(100, (pts_poss - def_pts_poss + 2.0) / 4.0 * 100))
    else:
        ppd_norm = 50.0

    # Weighted on-field efficiency bucket
    eff_norm = (
        sr_norm * 0.28 +
        epa_norm * 0.24 +
        ppo_norm * 0.16 +
        trench_norm * 0.16 +
        ppd_norm * 0.16
    )

    # Weighted composite (0-100) — weights come from COMPOSITE_CONFIG (ONE source
    # of truth, hashed into model_version). Numbers are identical to the previous
    # inline literals while fcs_rating/massey sit at 0.0, so this refactor is
    # behaviour-preserving; it just makes a weight change auditable.
    cfg = composite_config()
    _assert_weights_sum(cfg)
    # `fcs_rating` IS A POLL RANK (1..25, weekly, FCS Coaches Poll) — NOT a 0-100
    # strength score. See D1_COMPOSITE_PROPOSAL.md. Handing a rank to a composite
    # weight would INVERT the signal (rank 25 is weaker, not stronger), so this
    # slot stays deliberately inert. The composite is an FBS board; the correct
    # lever for FCS opponents is the missing-data prior: see
    # `fcs_composite_for()` — FCS_COMPOSITE_RANKED 33.0 / FCS_COMPOSITE_UNRANKED 15.0
    # / FCS_COMPOSITE_FALLBACK 19.0 by Massey rank. Targets measured from 848 real
    # FBS-vs-FCS games (FBS wins by 31.9 vs unranked, 14.2 vs ranked); mean absolute
    # error measured on 103 completed games: 21.5 -> 13.6 pts.
    # NOTE: 848 = the games the target margins came from; 103 = the games the error
    # was measured on. They are different datasets — do not conflate them.
    # (This used to read "a flat 16.0 for every FCS team" — that constant was
    # replaced; the comment outlived it and was corrected 2026-09-22.)
    fcs_rank = team_data.get("fcs_rating")
    try:
        fcs_norm = (50.0 if fcs_rank is None
                    else max(0.0, min(100.0, (26.0 - float(fcs_rank)) / 25.0 * 100.0)))
    except (TypeError, ValueError):
        fcs_norm = 50.0
    composite = (
        sp_norm * cfg["sp_plus"] +
        fpi_norm * cfg["fpi"] +
        srs_norm * cfg["srs"] +
        elo_norm * cfg["elo"] +
        talent_norm * cfg["talent"] +
        eff_norm * cfg["efficiency"] +
        fcs_norm * cfg["fcs_rating"] +
        50.0 * cfg["massey"]
    )

    # Projected score: calibrated to realistic CFB scoring.
    # A team's projected points vs an average opponent ranges ~8 (worst) to ~50s (elite).
    # League average is ~28 PPG. Composite 50 (average) -> ~27 pts.
    # points = 27 + (composite - 50) * 0.55, floored at 6.
    base_score = 27.0 + (composite - 50.0) * 0.55
    base_score = max(6.0, base_score)

    # Turnover regression: regressing turnover margin 50% toward 0 prevents overfitting to fumble/luck
    to_margin = team_data.get("turnover_margin") or 0.0
    regressed_to = to_margin * 0.5
    net_ppg = (off_ppg or 28.0) - (def_ppg or 24.0) - (to_margin - regressed_to) * 1.5
    base_score += max(-4.0, min(4.0, net_ppg * 0.15))

    # Home field advantage: ~2.5 points (CFBD research average)
    home_adj = 2.5 if is_home else -1.5
    projected_score = round(base_score + home_adj, 1)

    # OPPONENT-ADJUSTED SUPPRESSION (Aug 30, user call: "52-0, 53-7 are common"):
    # A bad team facing an elite defense scores far fewer points than its
    # base suggests. Scale points down by composite gap: each 10 pts of
    # (opp_composite - composite) above 30 cuts the offense by ~12%.
    if opp_composite is not None and (opp_composite - composite) > 30:
        suppression = 1.0 - min(0.75, (opp_composite - composite - 30) * 0.012)
        projected_score = round(projected_score * suppression, 1)

    # Points can't be negative — floor projected score at 0 (defensive floor,
    # not a normalization of the model output).
    if projected_score < 0:
        projected_score = 0.0

    # Win probability from composite (logistic model)
    # Composite 50 = 50%, 70 = ~88%, 30 = ~12%
    win_prob = 1 / (1 + (2.718 ** (-0.08 * (composite - 50))))
    win_prob = round(win_prob * 100, 1)

    return {
        "projected_score": projected_score,
        "composite": round(composite, 1),
        "win_probability": win_prob,
        "sp_contribution": round(sp_norm * 0.18, 1),
        "fpi_contribution": round(fpi_norm * 0.15, 1),
        "srs_contribution": round(srs_norm * 0.12, 1),
        "cpi_contribution": round(srs_norm * 0.12, 1),  # backwards compatibility alias
        "elo_contribution": round(elo_norm * 0.08, 1),
        "rec_contribution": round(talent_norm * 0.10, 1),
        "epa_contribution": round(eff_norm * 0.37, 1),  # backwards compatibility alias
        "efficiency_contribution": round(eff_norm * 0.37, 1),
        "sr_norm": round(sr_norm, 1),
        "trench_norm": round(trench_norm, 1),
        "ppo_norm": round(ppo_norm, 1),
    }





def _load_active_injuries() -> dict[str, dict]:
    """Load active team injuries from disk cache (team_name -> injury info)."""
    if ACTIVE_INJURIES_FILE.exists():
        try:
            with open(ACTIVE_INJURIES_FILE, "r") as f:
                data = json.load(f)
                return data.get("teams", {})
        except Exception as e:
            print(f"[injuries] load failed: {e}")
    return {}


def project_head_to_head(
    home_data: dict,
    away_data: dict,
    neutral_site: bool = False,
    home_injury_adj: float = 0.0,
    away_injury_adj: float = 0.0,
    wind_penalty: float = 0.0,
) -> dict:
    """Head-to-head projection: TOTAL from combined strength, MARGIN from composite gap.

    Aug 30 recalibration (user call): old independent-score model produced
    84-point totals between two elite teams and only 8-point margins for
    Minnesota-Eastern Illinois. Real CFB: elite totals sit ~52-58, blowouts
    52-0 / 53-7 are common, average totals ~48-52.

    Jeff Tracy rule: Star QB out is massive — 10.0 points deduction at star level.
    Persistent injury adjustments modify the game margin and total directly.

    total  = 51 + (avg_composite - 50) * 0.10   # elite games trend slightly higher
    margin = 1.0 * (comp_home - comp_away) + HFA (2.5 home / 0 neutral)
             — 1.0 pts margin per composite point: comp gap 40 -> ~38.5 pt margin
               (matches real 40+ spreads: OSU -51 vs Ball State etc.)
    home_score = (total + margin) / 2, away_score = (total - margin) / 2, floor 3.

    NOTE for anyone auditing a projected margin from the API: `differential` is NOT
    simply (composite gap + HFA). Three further terms apply, so a closed-form check
    will show residuals that are NOT bugs:
      * net_injury  — (home_injury_adj - away_injury_adj), Jeff's star-QB rule
      * wind_penalty — total only
      * underdog floor — when the composite gap exceeds 30 the weak side's share of
        the total is capped, which REWRITES home_score/away_score and therefore
        moves `differential` after margin was computed
    """
    hp = project_score_multi_factor(home_data, is_home=True)
    ap = project_score_multi_factor(away_data, is_home=False)
    hc, ac = hp["composite"], ap["composite"]
    # FBS-vs-FCS blowout boost REMOVED (Jeff, Sep 22 2026).
    # It was a stop-gap from when the FCS prior was a flat composite 16: week-1
    # margins came in ~10-30 pts under books' -40..-55 lines, so +15 was added.
    # Now that the composite ITSELF carries the FBS/FCS gap (15 unranked / 33
    # ranked, fitted from 103 real games), the boost double-counted: an average FBS
    # team vs an FCS team read (50-16)+2.5+15 = 51.5 against a measured 37.2.
    # Kept as a named zero rather than deleted so any lingering reference is
    # obvious, and so re-adding a gap correction is a one-line change if the
    # fitted composite later proves too tight.
    FCS_BLOWOUT_BOOST = 0.0
    home_fcs = hp.get("data_flag") == "fcs_no_data"
    away_fcs = ap.get("data_flag") == "fcs_no_data"
    boost = 0.0
    if FCS_BLOWOUT_BOOST:
        if home_fcs and not away_fcs:
            boost = -FCS_BLOWOUT_BOOST   # away (FBS) gains
        elif away_fcs and not home_fcs:
            boost = +FCS_BLOWOUT_BOOST   # home (FBS) gains
    avg = (hc + ac) / 2.0
    total = 51.0 + (avg - 50.0) * 0.10 + boost
    # Calibrated margin curve: 1.0 pt of margin per 1.0 pt of composite gap
    # Replaces the legacy 0.45 compression that artificially suppressed favorites
    gap_ = hc - ac
    margin = gap_ * 1.0
    if not neutral_site:
        margin += 2.5  # HFA
    margin += boost  # boost widens the margin in the FBS side's favor

    # Persistent injury adjustment (Jeff Tracy rule: Star QB out = -10.0 pts):
    # home_injury_adj and away_injury_adj are negative numbers (e.g. -10.0)
    net_injury = (home_injury_adj or 0.0) - (away_injury_adj or 0.0)
    margin += net_injury

    # Offensive drop-off also reduces expected game total
    total_injury = ((home_injury_adj or 0.0) + (away_injury_adj or 0.0)) * 0.70
    total = max(24.0, total + total_injury)

    # Wind penalty on total: high sustained winds (>=15 mph) impair kicking and deep passing
    if wind_penalty > 0.0:
        total = max(24.0, total - wind_penalty)

    # Split total by margin. For lopsided games the underdog's share bottoms
    # out near the "garbage time" floor: 52-0 / 53-7 finals are common, so a
    # 35+ pt underdog gets ~10% of the total, not a symmetric 50/50 split.
    home_score = (total + margin) / 2.0
    away_score = (total - margin) / 2.0
    # Underdog floor: composite gap > 30 caps the weak side's share of total.
    gap = abs(hc - ac)
    floor_share = 0.50 - min(0.40, max(0.0, (gap - 30)) * 0.011)  # gap 66 -> 0.06
    if away_score < home_score:
        max_away = total * floor_share
        if away_score > max_away:
            # shave the underdog, give the difference to the favorite
            home_score += away_score - max_away
            away_score = max_away
    else:
        max_home = total * floor_share
        if home_score > max_home:
            away_score += home_score - max_home
            home_score = max_home
    home_score = max(3.0, round(home_score, 1))
    away_score = max(3.0, round(away_score, 1))
    return {
        "home_proj": home_score,
        "away_proj": away_score,
        "differential": round(home_score - away_score, 1),
        "home_composite": hc,
        "away_composite": ac,
        "home_win_probability": hp["win_probability"],
        "away_win_probability": ap["win_probability"],
        "total": round(home_score + away_score, 1),
        "home_injury_adj": round(home_injury_adj or 0.0, 1),
        "away_injury_adj": round(away_injury_adj or 0.0, 1),
        "wind_penalty": round(wind_penalty or 0.0, 1),
    }

def fetch_live_analytics():
    """Fetch and merge FPI, SP+, Recruiting, SRS, Elo, and REAL season stats from CFBD API."""
    try:
        teams_db = _cfbd_teams()
        fpi_data = _cfbd_fpi()
        sp_data = _cfbd_sp()
        rec_data = _cfbd_recruiting()
        talent_data = _cfbd_talent()
        srs_data = _cfbd_srs()
        elo_data = _cfbd_elo()
        real_stats = _cfbd_season_stats()

        # EPA (Expected Points Added) + possession-based metrics from CFBD
        ppa_data = _cfbd_ppa()
        adv_data = _cfbd_advanced_stats()
        drive_stats = _cfbd_drives_for_teams(list(
            teams_db.keys() | fpi_data.keys() | sp_data.keys() | rec_data.keys()))
        returning_data = _cfbd_returning()
        roster_exp = _cfbd_roster_experience()
        records = _cfbd_records()  # real W/L, not hardcoded zeros
        conf_map = load_fbs_conferences()
        all_names = set(list(teams_db.keys()) + list(fpi_data.keys()) + list(rec_data.keys()))
        analytics = []

        # Sort by FPI rank (primary), then recruiting rank
        def sort_key(name):
            fpi_rank = fpi_data.get(name, {}).get("ranking", 999)
            rec_rank = rec_data.get(name, {}).get("rank", 999)
            return (fpi_rank, rec_rank)

        for name in sorted(all_names, key=sort_key):
            fpi = fpi_data.get(name, {})
            sp = sp_data.get(name, {})
            rec = rec_data.get(name, {})
            srs = srs_data.get(name, {})
            elo = elo_data.get(name, {})
            team_info = teams_db.get(name, {})
            st = real_stats.get(name, {})  # REAL season stats (or {} if unavailable)

            # Core ratings
            fpi_score = fpi.get("fpi", 0)
            fpi_rank = fpi.get("ranking", 0)
            sp_plus = sp.get("rating", 0)
            sp_offense = sp.get("offense", {}).get("rating", 0) if isinstance(sp.get("offense"), dict) else sp.get("offense", 0)
            sp_defense = sp.get("defense", {}).get("rating", 0) if isinstance(sp.get("defense"), dict) else sp.get("defense", 0)
            sp_rank = sp.get("ranking", 0)
            srs_score = srs.get("rating", 0)
            elo_rating = elo.get("elo", 0)

            # Recruiting & 247 Team Talent Composite
            rec_rank = rec.get("rank", 0)
            rec_points = rec.get("points", 0)
            talent_score = talent_data.get(name, {}).get("talent")

            # Derived metrics (these are model estimates — CFBD has no true FPI win prob / CPI)
            fpi_win_prob = max(0, min(100, 50 + fpi_score * 1.5))
            cpi = max(0, min(100, 50 + sp_plus * 2.0)) if sp_plus else 50.0
            coach_win_pct = max(0.5, min(0.95, 0.85 - (rec_rank - 1) * 0.005)) if rec_rank else 0.75

            # REAL season stats (fall back to neutral values only when CFBD has no data)
            off_ppg = st.get("off_ppg")
            def_ppg = st.get("def_ppg")
            off_ypp = st.get("off_ypp")
            def_ypp = st.get("def_ypp")
            off_3rd = st.get("off_3rd")
            def_3rd = st.get("def_3rd")
            turnover_margin = st.get("turnover_margin")
            # HAVOC (Sep 6): defensive havoc rate + havoc our offense allows
            def_havoc = st.get("def_havoc")
            havoc_allowed = st.get("havoc_allowed")

            # EPA (Expected Points Added) + possession-based metrics from CFBD
            ppa = ppa_data.get(name, {})
            adv = adv_data.get(name, {})
            ds = drive_stats.get(name, {})
            epa_overall = ppa.get("epa_play")
            epa_pass = ppa.get("epa_pass")
            epa_rush = ppa.get("epa_rush")
            def_epa_overall = ppa.get("def_epa_play")
            def_epa_pass = ppa.get("def_epa_pass")
            def_epa_rush = ppa.get("def_epa_rush")
            # Advanced season stats (Success Rate, PPO, Line Yards, Stuff Rate)
            off_sr = adv.get("off_success_rate")
            def_sr = adv.get("def_success_rate")
            off_ppo = adv.get("off_ppo")
            def_ppo = adv.get("def_ppo")
            off_ly = adv.get("off_line_yards")
            def_ly = adv.get("def_line_yards")
            off_st = adv.get("off_stuff_rate")
            def_st = adv.get("def_stuff_rate")
            off_power = adv.get("off_power_success")
            def_power = adv.get("def_power_success")
            off_explosiveness = adv.get("off_explosiveness")
            def_explosiveness = adv.get("def_explosiveness")
            pts_per_poss = ds.get("pts_per_poss")
            td_rate = ds.get("td_rate")
            fg_rate = ds.get("fg_rate")
            to_rate = ds.get("turnover_rate")
            def_pts_per_poss = ds.get("def_pts_per_poss")
            def_td_rate = ds.get("def_td_rate")
            def_fg_rate = ds.get("def_fg_rate")
            def_turnover_created = ds.get("def_turnover_created")
            # Returning production + roster experience (portal impact) from CFBD
            ret = returning_data.get(name, {})
            exp = roster_exp.get(name, {})
            returning_ppa = ret.get("totalPPA")
            pct_ppa_returning = ret.get("percentPPA")
            pct_pass_ppa = ret.get("percentPassingPPA")
            pct_rush_ppa = ret.get("percentRushingPPA")
            roster_count = exp.get("roster_count")
            avg_year = exp.get("avg_year")
            experience_score = exp.get("experience_score")

            analytics.append({
                "rank": fpi_rank,
                "name": name,
                "mascot": team_info.get("nickname", ""),
                "conf": conf_map.get(name, team_info.get("conference", "FBS")),
                "emoji": "🏈",
                # Real W/L from CFBD /records (2026 has live data in-season;
                # pre-season every team is correctly 0-0).
                "wins": records.get(name, {}).get("wins", 0),
                "losses": records.get(name, {}).get("losses", 0),
                "points": fpi_score,
                "sp_plus": round(sp_plus, 2),
                "sp_offense": round(sp_offense, 1),
                "sp_defense": round(sp_defense, 1),
                "sp_rank": sp_rank,
                "fpi": round(fpi_score, 2),
                "fpi_rank": fpi_rank,
                "fpi_win_prob": round(fpi_win_prob, 1),
                "cpi": round(cpi, 1),
                "srs": round(srs_score, 2),
                # Which season this value came from. Without it the UI cannot tell a
                # current-season rating from a fallback — which is how a 2025 SRS was
                # displayed as current while carrying 12% of the composite weight.
                "srs_source_year": RATING_SOURCE_YEARS.get("srs"),
                "elo": round(elo_rating, 1),
                "recruiting_rank": rec_rank,
                "recruiting_pts": round(rec_points, 2),
                "talent_score": round(talent_score, 1) if talent_score else None,
                "recruiting_commits": 0,
                "recruiting_5star": 0,
                "recruiting_4star": 0,
                "coach_win_pct": round(coach_win_pct, 2),
                "off_ppg": off_ppg,
                "off_ypp": off_ypp,
                "off_3rd": off_3rd,
                "def_ppg": def_ppg,
                "def_ypp": def_ypp,
                "def_3rd": def_3rd,
                "turnover_margin": turnover_margin,
                "def_havoc": def_havoc,
                "havoc_allowed": havoc_allowed,
                "epa_play": epa_overall,
                "epa_pass": epa_pass,
                "epa_rush": epa_rush,
                "pts_per_poss": pts_per_poss,
                "td_rate": td_rate,
                "fg_rate": fg_rate,
                "turnover_rate": to_rate,
                "def_epa_play": def_epa_overall,
                "def_epa_pass": def_epa_pass,
                "def_epa_rush": def_epa_rush,
                "def_pts_per_poss": def_pts_per_poss,
                "def_td_rate": def_td_rate,
                "def_fg_rate": def_fg_rate,
                "def_turnover_created": def_turnover_created,
                "off_success_rate": off_sr,
                "def_success_rate": def_sr,
                "off_ppo": off_ppo,
                "def_ppo": def_ppo,
                "off_line_yards": off_ly,
                "def_line_yards": def_ly,
                "off_stuff_rate": off_st,
                "def_stuff_rate": def_st,
                "off_power_success": off_power,
                "def_power_success": def_power,
                "off_explosiveness": off_explosiveness,
                "def_explosiveness": def_explosiveness,
                "returning_ppa": round(returning_ppa, 1) if returning_ppa else None,
                "pct_ppa_returning": round(pct_ppa_returning * 100, 1) if pct_ppa_returning else None,
                "pct_pass_ppa": round(pct_pass_ppa * 100, 1) if pct_pass_ppa else None,
                "pct_rush_ppa": round(pct_rush_ppa * 100, 1) if pct_rush_ppa else None,
                "roster_count": roster_count,
                "avg_year": avg_year,
                "experience_score": experience_score,
                "movement": 0,
                "streak": "—",
            })

        print(f"[+] CFBD analytics: {len(analytics)} teams (FPI:{len(fpi_data)} SP+:{len(sp_data)} REC:{len(rec_data)} SRS:{len(srs_data)} ELO:{len(elo_data)} STATS:{len(real_stats)} PPA:{len(ppa_data)} DRIVES:{len(drive_stats)} RET:{len(returning_data)} ROSTER:{len(roster_exp)})")
        return analytics
    except Exception as e:
        print(f"[CFBD analytics fetch failed] {e}")
        return None

@app.post("/api/analytics/fetch", dependencies=_ADMIN)
def api_analytics_fetch():
    """Force-fetch live analytics data from CFBD API and persist to disk.

    Ops-only: the Analytics page's manual refresh button was removed (the server
    keeps the data current on its own — hourly line refresh plus the Sun/Mon/Tue/
    Wed 9pm PT anchor), so nothing public calls this any more and it carries the
    admin gate like the other mutating routes. The 300s throttle stays as a
    backstop for authorised callers."""
    import traceback
    cached = _cache_get(_analytics_cache, ANALYTICS_FETCH_TTL)
    if cached:
        return {"status": "throttled", "source": "cache",
                "teams": len(cached.get("teams", [])), "updated": cached.get("updated"),
                "note": f"live fetch suppressed — analytics pulled <{ANALYTICS_FETCH_TTL}s ago"}
    try:
        # Do NOT touch the rankings cache — analytics lives in its own store
        analytics = fetch_live_analytics()
        if analytics:
            _save_rating_vintages()
            # Persist to disk so the team map and future reads use fresh data
            try:
                with open(_CFBD_ANALYTICS_FILE, "w") as f:
                    json.dump(analytics, f, indent=2)
            except Exception as e:
                print(f"[!] Could not persist analytics to disk: {e}")
            _enrich_with_composite(analytics)
            result = {
                "week": datetime.now().strftime("%B %d, %Y"),
                "season": CFBD_YEAR,
                "updated": datetime.now().isoformat(),
                "teams": analytics,
                "source": "cfbd",
            }
            _cache_set(_analytics_cache, result)
            # Also refresh the team map cache so schedule/rankings see new metrics
            _build_team_map()
            return {"status": "fetched", "source": "cfbd", "teams": len(analytics)}
        else:
            # Live fetch failed — fall back to disk cache instead of crashing.
            # This happens when CFBD rate-limits (429) or is temporarily down;
            # existing cached data on disk is still valid for serving requests.
            cached = _load_cfbd_analytics_file()
            if cached:
                print("[!] Live fetch failed; serving from disk cache (%d teams)" % len(cached))
                _enrich_with_composite(cached)
                result = {
                    "week": datetime.now().strftime("%B %d, %Y"),
                    "season": CFBD_YEAR,
                    "updated": datetime.now().isoformat(),
                    "teams": cached,
                    "source": "cfbd-disk-cache",
                }
                _cache_set(_analytics_cache, result)
                _build_team_map()
                return {"status": "cached", "source": "cfbd-disk-cache",
                        "teams": len(cached), "note": "Live fetch failed; served from disk cache"}
            raise HTTPException(502, "CFBD analytics fetch failed and no disk cache available")
    except HTTPException:
        raise
    except Exception as e:
        print(f"[POST /api/analytics/fetch ERROR] {e}")
        traceback.print_exc()
        raise HTTPException(500, str(e))

@app.get("/api/analytics/pull-status")
def api_analytics_pull_status():
    """Read-only: when the weekly CFBD analytics sync (SP+/Elo/FPI/talent) last
    ran and whether the most recent Sun/Mon/Tue/Wed 21:00 PT anchor is still
    outstanding. Exposing this lets both environments be verified for refresh
    parity instead of inferred from cache build times."""
    ts = _load_last_analytics_pull()
    due_at = _most_recent_anchor_pt()
    now = time.time()
    return {
        "last_pull_utc": (datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None),
        "last_pull_age_hours": (round((now - ts) / 3600, 2) if ts else None),
        "due": weekly_analytics_due(),
        "anchors": [f"{('Mon','Tue','Wed','Thu','Fri','Sat','Sun')[wd]} {hr:02d}:00 PT"
                    for wd, hr in _ANALYTICS_ANCHORS],
        "most_recent_anchor_utc": (due_at.astimezone(timezone.utc).isoformat() if due_at else None),
        "scheduler_interval_seconds": REFRESH_INTERVAL_SECONDS,
    }


@app.post("/api/analytics/refresh-if-due", dependencies=_ADMIN)
def api_analytics_refresh_if_due():
    """Anchor-gated CFBD analytics pull — deterministic trigger for hosts whose
    background thread can sleep (e.g. Cloudflare Containers with sleepAfter).
    Idempotent: pulls only when the latest Sun/Mon/Tue/Wed 21:00 PT anchor
    postdates the last recorded pull, exactly like the in-process scheduler."""
    try:
        if not weekly_analytics_due():
            ts = _load_last_analytics_pull()
            return {"due": False, "pulled": False,
                    "last_pull_utc": (datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None)}
        res = run_weekly_analytics_pull()
        if res.get("pulled"):
            _save_last_analytics_pull(time.time())
        ts = _load_last_analytics_pull()
        return {"due": True, "pulled": bool(res.get("pulled")), "teams": res.get("teams", 0),
                "last_pull_utc": (datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None)}
    except Exception as e:
        print(f"[POST /api/analytics/refresh-if-due ERROR] {e}")
        raise HTTPException(500, str(e))

# ── CFBD Schedule Fetcher ──
# The Schedule page uses CFBD's full-season game list (the same source Win
# Totals already relies on) instead of ESPN's scoreboard — ESPN's week=N param
# silently truncates to 25 events (observed Aug 2026), which broke every week.
# CFBD returns the complete slate for all weeks in one call, with week numbers
# and ISO dates.
FBS_TEAMS_FILE = BASE_DIR / "data" / "fbs_teams.json"

def _cfbd_is_fbs(game: dict) -> bool:
    """True if either side of a CFBD game is FBS-classified."""
    return ((game.get("homeClassification") or "").upper().startswith("FBS")
            or (game.get("awayClassification") or "").upper().startswith("FBS"))


def fetch_cfbd_schedule(week: int = 1, year: int = CFBD_YEAR) -> list[dict] | None:
    """All FBS matchups for one week from the CFBD season game list.

    Returns [{home, away, date, neutral_site}] sorted by kickoff time, or [] if
    CFBD has no games published for that week yet (bye week / future week).
    None only on hard failure."""
    try:
        team_map = _build_team_map()
        known = set(team_map.keys())
        matchups = []
        for g in _cfbd_season_games(year):
            if not isinstance(g, dict) or g.get("week") != week:
                continue
            home_name = g.get("homeTeam") or ""
            away_name = g.get("awayTeam") or ""
            # FBS-only (same rule as Win Totals), and both teams must be in our
            # analytics set so the model can project them.
            if not _cfbd_is_fbs(g):
                continue
            if home_name not in known or away_name not in known:
                continue
            matchups.append({
                "home": home_name,
                "away": away_name,
                "game_id": g.get("id"),      # CFBD game id — keys model_predictions
                "date": g.get("startDate"),  # ISO 8601 UTC, e.g. 2026-08-29T16:00:00Z
                "neutral_site": bool(g.get("neutralSite")),
                "home_classification": (g.get("homeClassification") or "").upper(),
                "away_classification": (g.get("awayClassification") or "").upper(),
            })
        matchups.sort(key=lambda m: (m["date"] or ""))
        return matchups
    except Exception as e:
        print(f"[CFBD schedule fetch failed] {e}")
        return None


def cfbd_weeks(year: int = CFBD_YEAR) -> list[int]:
    """Week numbers that have at least one FBS game in the season list."""
    weeks = set()
    for g in _cfbd_season_games(year):
        if isinstance(g, dict) and _cfbd_is_fbs(g) and isinstance(g.get("week"), int):
            weeks.add(g["week"])
    return sorted(weeks)


_current_week_cache: dict = {}

def current_season_week(year: int = CFBD_YEAR) -> int | None:
    """The season week the schedule page should show by default.

    Rule (user-corrected Sep 20 2026): the page shows the UPCOMING slate. A
    week becomes current 4 days before its first kickoff, floored to midnight
    ET — i.e. Sunday for the standard Thursday slate, the moment the prior
    week's games are done. (The old Monday 00:00 ET cutover left the page
    defaulting to a slate that had already been played all Sunday.) Derived
    from CFBD's real game dates, so bye weeks and calendar quirks are handled
    automatically. Before the first cutover -> earliest week; after the last
    one -> latest week. None if the season has no games.
    """
    cached = _current_week_cache.get(year)
    if cached is not None and time.time() - cached[1] < 3600:
        # Fresh cache: serve it. Stale cache: fall through and RECOMPUTE.
        # (The old condition inverted this — a stale cache returned None,
        # which the endpoint read as "no games" and fell back to weeks[0],
        # pinning the schedule page to week 1 an hour after every deploy.)
        return cached[0]
    games = [g for g in _cfbd_season_games(year)
             if isinstance(g, dict) and _cfbd_is_fbs(g) and g.get("startDate")]
    if not games:
        return None
    earliest_by_week: dict[int, datetime] = {}
    for g in games:
        try:
            start = datetime.fromisoformat(g["startDate"].replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        wk = g.get("week")
        if not isinstance(wk, int):
            continue
        if wk not in earliest_by_week or start < earliest_by_week[wk]:
            earliest_by_week[wk] = start
    if not earliest_by_week:
        return None
    ET = timezone(timedelta(hours=-4))  # EDT; cutover precision of a day makes DST irrelevant
    now = datetime.now(timezone.utc)
    switch_points = []
    for wk, start in earliest_by_week.items():
        local = start.astimezone(ET)
        # Cutover = 4 days before this week's first kickoff, floored to 00:00 ET
        # (Sunday for the standard Thu-Sat slate): the upcoming week becomes
        # current as soon as the previous week's games are in the books.
        cutover = (local - timedelta(days=4)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        switch_points.append((cutover, wk))
    switch_points.sort()
    current = switch_points[0][1]  # before/at first switch -> earliest week
    for point, wk in switch_points:
        if now >= point:
            current = wk
    _current_week_cache[year] = (current, time.time())
    return current


@app.get("/api/schedule/current-week")
def api_schedule_current_week(year: int = CFBD_YEAR):
    """Week the schedule page should default to (upcoming-slate rule)."""
    try:
        wk = current_season_week(year)
        weeks = cfbd_weeks(year)
        if wk is None:
            wk = weeks[0] if weeks else 1
        return {"year": year, "week": wk, "weeks": weeks}
    except Exception as e:
        print(f"[GET /api/schedule/current-week ERROR] {e}")
        raise HTTPException(502, f"Current-week fetch failed: {e}")



def load_fbs_conferences() -> dict:
    """Load conference mapping from the FBS teams database.
    Keys by both displayName and location for flexible lookup.
    """
    try:
        with open(FBS_TEAMS_FILE) as f:
            teams = json.load(f)
        conf_map = {}
        for t in teams:
            conf_map[t["name"]] = t["conference"]
            if t.get("location"):
                conf_map[t["location"]] = t["conference"]
        return conf_map
    except Exception:
        return {}

# ── Schedule / Differentials ──
def load_schedule() -> dict:
    try:
        with open(SCHEDULE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"week": 0, "season": 2026, "matchups": []}

_TEAM_MAP_CACHE = {}
_TEAM_MAP_CACHE_TS = 0
_LOGO_MAP = {}  # team_name_lower -> logo_url
# Restore logo map from disk on hot-reload
_LOGO_FILE = BASE_DIR / "data" / "cfbd_logos.json"
if _LOGO_FILE.exists():
    with open(_LOGO_FILE) as f:
        _LOGO_MAP = json.load(f)
_CFBD_ANALYTICS_FILE = BASE_DIR / "data" / "cfbd_analytics.json"

def _load_cfbd_analytics_file():
    """Load pre-fetched CFBD analytics from disk."""
    try:
        with open(_CFBD_ANALYTICS_FILE) as f:
            return json.load(f)
    except Exception:
        return []

def _build_team_map() -> dict:
    """Build team map from local data + pre-fetched CFBD analytics + CFBD team logos.
    Caches the result (60s TTL) so repeated calls within requests don't re-read disk.
    """
    global _TEAM_MAP_CACHE_TS
    if _TEAM_MAP_CACHE and time.time() - _TEAM_MAP_CACHE_TS < 60:
        return _TEAM_MAP_CACHE
    teams = load_local()
    team_map = {t["name"]: dict(t) for t in teams}
    # Load CFBD analytics from pre-fetched file (not live API)
    try:
        cfbd_analytics = _load_cfbd_analytics_file()
        if cfbd_analytics:
            for t in cfbd_analytics:
                name = t["name"]
                if name not in team_map:
                    team_map[name] = t
                else:
                    # Merge CFBD metrics into existing local team.
                    # CFBD is the source of truth for analytics: prefer non-zero CFBD values
                    # over anything stale already present.
                    for key in ["sp_plus", "sp_offense", "sp_defense", "sp_rank",
                               "fpi", "fpi_rank", "fpi_win_prob", "cpi", "srs", "elo",
                               "recruiting_rank", "recruiting_pts", "coach_win_pct",
                               "off_ppg", "off_ypp", "off_3rd", "def_ppg", "def_ypp",
                               "def_3rd", "turnover_margin",
                               "epa_play", "epa_pass", "epa_rush",
                               "pts_per_poss", "td_rate", "fg_rate", "turnover_rate",
                               "def_epa_play", "def_epa_pass", "def_epa_rush",
                               "def_pts_per_poss", "def_td_rate", "def_fg_rate", "def_turnover_created",
                    "returning_ppa", "pct_ppa_returning", "pct_pass_ppa", "pct_rush_ppa",
                    "roster_count", "avg_year", "experience_score"]:
                        if key in t and t[key] not in (None, 0, ""):
                            team_map[name][key] = t[key]
    except Exception as e:
        print(f"[TEAM_MAP ERROR] {e}")
        pass  # Fall back to local-only
    # Live W/L overlay: schedule-page team records must track actual results,
    # not the (formerly hardcoded 0-0) analytics snapshot.
    try:
        _overlay_records(list(team_map.values()))
    except Exception as e:
        print(f"[TEAM_MAP records overlay failed] {e}")
    # Merge logos from startup cache (avoids live CFBD call per request)
    for name in list(team_map.keys()):
        logo = _LOGO_MAP.get(name.lower())
        if logo:
            team_map[name]["logo_url"] = logo
    _TEAM_MAP_CACHE.clear()
    _TEAM_MAP_CACHE.update(team_map)
    _TEAM_MAP_CACHE_TS = time.time()
    return _TEAM_MAP_CACHE

@app.get("/api/schedule")
def api_schedule():
    """Get weekly schedule with projected differentials + live betting lines."""
    sched = load_schedule()
    team_map = _build_team_map()
    injuries_map = _load_active_injuries()
    # Fetch live odds once (PropLine primary, The Odds API backup, CFBD fallback)
    odds_map = _fetch_odds_map()
    enriched = []
    for m in sched.get("matchups", []):
        home = team_map.get(m["home"], {})
        away = team_map.get(m["away"], {})
        home_inj_data = injuries_map.get(m["home"], {})
        away_inj_data = injuries_map.get(m["away"], {})
        home_injury_adj = home_inj_data.get("net_injury_points", 0.0)
        away_injury_adj = away_inj_data.get("net_injury_points", 0.0)
        # Multi-factor projection
        home_proj_data = project_score_multi_factor(home, is_home=True)
        away_proj_data = project_score_multi_factor(away, is_home=False)
        # Second pass with opponent suppression (great defenses hold bad teams
        # to single digits — 52-0 and 53-7 finals are common in CFB).
        home_proj_data = project_score_multi_factor(
            home, is_home=True, opp_composite=away_proj_data["composite"])
        away_proj_data = project_score_multi_factor(
            away, is_home=False, opp_composite=home_proj_data["composite"])
        base_diff = round(home_proj_data["projected_score"] - away_proj_data["projected_score"], 1)
        # Apply persistent injury adjustment:
        diff = round(base_diff + (home_injury_adj - away_injury_adj), 1)
        # Look up live betting line (negative spread = home is favorite)
        odds_key = (_normalize_team_name(m["home"]), _normalize_team_name(m["away"]))
        found_key = _find_odds_entry(odds_map, *odds_key, m.get("date") or "")
        line = odds_map.get(found_key, {}) if found_key else {}
        spread = line.get("spread")  # None if no live line; negative = home favorite
        # line_diff uses the same sign as the spread: negative = home favorite
        line_diff = round(spread, 1) if spread is not None else None
        # Comparison: how the live line differs from our model differential
        # diff > 0 = home favored; spread < 0 = home favored. Both same direction → diff + spread
        line_vs_model = round(diff + (spread or 0), 1) if spread is not None else None
        # ATS pick: which side to take against the spread.
        # Home covers if home wins by MORE than |spread|. If model margin is
        # under the spread, the underdog is the ATS side.
        ats_pick = _ats_side(diff, spread)
        # Over/Under: model total vs book total
        model_total, total_vs_model, over_pick = _totals_fields(
            home_proj_data["projected_score"], away_proj_data["projected_score"],
            line.get("total"))
        enriched.append({
            **m,
            "home_proj": home_proj_data["projected_score"],
            "away_proj": away_proj_data["projected_score"],
            "differential": diff,
            "home_favorite": diff > 0,
            "projected_winner": "home" if diff > 0 else ("away" if diff < 0 else None),
            "ats_pick": ats_pick,
            "home_composite": home_proj_data["composite"],
            "away_composite": away_proj_data["composite"],
            "home_win_prob": home_proj_data["win_probability"],
            "away_win_prob": away_proj_data["win_probability"],
            "home_sp": home.get("sp_plus", 0),
            "away_sp": away.get("sp_plus", 0),
            "home_record": f"{home.get('wins',0)}-{home.get('losses',0)}",
            "away_record": f"{away.get('wins',0)}-{away.get('losses',0)}",
            "home_conf": home.get("conf", ""),
            "away_conf": away.get("conf", ""),
            "home_injury_adj": home_injury_adj,
            "away_injury_adj": away_injury_adj,
            "home_injuries": home_inj_data.get("injuries", []),
            "away_injuries": away_inj_data.get("injuries", []),
            "home_logo_url": home.get("logo_url"),
            "away_logo_url": away.get("logo_url"),
            # Live betting line (negative = home favorite, positive = underdog)
            "line_diff": line_diff,
            "line_total": line.get("total"),
            "line_source": line.get("source"),
            # How much the live line diverges from our model
            "line_vs_model": line_vs_model,
            # Over/under projection
            "model_total": model_total,
            "total_vs_model": total_vs_model,
            "over_pick": over_pick,
        })
    # Lock in this week's SU + ATS picks (idempotent)
    _lock_picks(enriched, sched.get("week", 1))
    return {**sched, "matchups": enriched}

@app.get("/api/odds")
def api_odds():
    """Get live betting odds for NCAAF (PropLine primary, The Odds API + CFBD backup)."""
    try:
        odds_map = _fetch_odds_map()
        odds = []
        for (home, away), v in odds_map.items():
            odds.append({
                "home": home,
                "away": away,
                "spread": v.get("spread"),
                "total": v.get("total"),
                "source": v.get("source"),
                "book": v.get("book"),
                "book_title": v.get("book_title"),
            })
        return {
            "odds": odds,
            "count": len(odds),
            "updated": datetime.now().isoformat(),
        }
    except Exception as e:
        print(f"[GET /api/odds ERROR] {e}")
        return {"error": str(e), "odds": []}


@app.get("/api/projections")
def api_projections():
    """Get multi-factor projections for ALL FBS teams + live betting odds overlay."""
    try:
        # Use the full FBS analytics set (687 teams), not just the 25 ranked
        # teams, so the frontend's "Bottom 10" shows the actual worst teams.
        all_teams = _load_cfbd_analytics_file()
        if not all_teams:
            # Fallback to ranked teams if analytics file is missing
            rankings = get_rankings()
            all_teams = [t.model_dump() if hasattr(t, 'model_dump') else t.dict()
                         for t in rankings.teams]
        projections = []
        for td in all_teams:
            home_proj = project_score_multi_factor(td, is_home=True)
            away_proj = project_score_multi_factor(td, is_home=False)
            projections.append({
                **td,
                "home_projection": home_proj,
                "away_projection": away_proj,
            })
        # Sort by composite (home) descending
        projections.sort(key=lambda x: x["home_projection"]["composite"], reverse=True)
        return {"projections": projections, "count": len(projections)}
    except Exception as e:
        print(f"[GET /api/projections ERROR] {e}")
        return {"error": str(e), "projections": []}


@app.post("/api/schedule/update", dependencies=_ADMIN)
def api_schedule_update(matchups: list[dict]):
    """Update weekly matchups. Validates input to prevent junk/XSS in the schedule file."""
    if len(matchups) > 200:
        raise HTTPException(400, "Too many matchups (max 200)")
    clean = []
    for m in matchups:
        home = str(m.get("home", "")).strip()
        away = str(m.get("away", "")).strip()
        # Reject empty or overly long names, and anything containing HTML/script
        if not home or not away:
            raise HTTPException(400, "Matchup missing home or away team")
        if len(home) > 60 or len(away) > 60:
            raise HTTPException(400, "Team name too long")
        if any(c in home + away for c in "<>\"'`"):
            raise HTTPException(400, "Invalid characters in team name")
        clean.append({"home": home, "away": away})
    sched = load_schedule()
    sched["matchups"] = clean
    sched["updated"] = datetime.now().isoformat()
    with open(SCHEDULE_FILE, "w") as f:
        json.dump(sched, f, indent=2)
    return {"status": "updated", "count": len(clean)}

_PRED_LOCK = {"ts": 0.0}


def _maybe_write_predictions(games: list[dict]) -> int:
    """Write model_predictions for un-started games — at most once per hour.

    Risk register D2: predictions must exist BEFORE kickoff, never post-hoc. The
    margin/total/probability here are exactly the head-to-head numbers the page
    displays, so the archive records what the model actually said; the writer
    then rejects anything whose kickoff has passed.

    Idempotent: insert_model_predictions replaces on (game_id, model_version).
    """
    now = time.time()
    if now - _PRED_LOCK["ts"] < 3600:      # throttle: one attempt per hour
        return 0
    _PRED_LOCK["ts"] = now
    if not d1_write_path.enabled():
        return 0
    rows = []
    for m in games:
        home_proj, away_proj = m.get("home_proj"), m.get("away_proj")
        if home_proj is None or away_proj is None or not m.get("date"):
            continue
        rows.append({
            "game_id": m.get("game_id"),
            "date": m.get("date"),
            "predicted_margin_home": m.get("differential"),
            "predicted_total": round(float(home_proj) + float(away_proj), 1),
            "win_prob_home": m.get("home_win_prob"),
        })
    n = d1_write_path.snapshot_predictions(rows, model_version=composite_version())
    if n:
        print(f"[Schedule] D1 model_predictions: {n} rows written pre-kickoff")
    return n


@app.post("/api/schedule/fetch")
def api_schedule_fetch(week: int = 1, year: int = 2026):
    """Fetch the full FBS slate for a week from CFBD and project scores.

    Source is CFBD's season game list (same as Win Totals) — ESPN's scoreboard
    was dropped after its week=N param started truncating to 25 events.

    Called by the Schedule page on load, so it is throttled rather than
    token-gated: a repeat call for the same week inside SCHEDULE_FETCH_TTL
    reuses the cached payload instead of re-hitting CFBD + the odds feeds."""
    _key = (week, year)
    _hit = _SCHEDULE_FETCH_CACHE.get(_key)
    if _hit and time.time() - _hit["ts"] < SCHEDULE_FETCH_TTL:
        _cached = dict(_hit["data"])
        _cached["cached"] = True
        _cached["note"] = ((_cached.get("note") or "") + " (reused from cache)").strip()
        return _cached
    matchups = fetch_cfbd_schedule(week, year)
    if matchups is None:
        raise HTTPException(502, "CFBD schedule fetch failed")
    note = None
    if not matchups:
        # Bye week or a future week CFBD hasn't published yet.
        note = (f"No FBS games are listed for Week {week} yet — "
                f"CFBD publishes schedules as the season approaches.")
    team_map = _build_team_map()
    conf_map = load_fbs_conferences()
    injuries_map = _load_active_injuries()
    weather_map = _cfbd_weather(week, year)

    # Fetch live betting odds (PropLine primary, The Odds API + CFBD backup)
    odds_map = _fetch_odds_map()

    enriched = []
    for m in matchups:
        home = dict(team_map.get(m["home"], {}))
        away = dict(team_map.get(m["away"], {}))
        # Pass CFBD classification through so the model applies an FCS prior to
        # no-data opponents (FCS squads defaulting to "average" gave 24 pts vs
        # Minnesota — Aug 30 fix).
        home["classification"] = m.get("home_classification") or home.get("classification") or ""
        away["classification"] = m.get("away_classification") or away.get("classification") or ""
        # FCS strength: a rating-derived composite replaces the old flat prior, so a
        # top-25 FCS opponent is no longer projected identically to a weak one. Only
        # applied when the side genuinely has no ratings of its own.
        if (home.get("classification") or "").upper() == "FCS" and not (home.get("sp_plus") or home.get("elo")):
            home["fcs_composite"] = fcs_composite_for(m["home"])
        if (away.get("classification") or "").upper() == "FCS" and not (away.get("sp_plus") or away.get("elo")):
            away["fcs_composite"] = fcs_composite_for(m["away"])
        # Active injury adjustment lookup (Jeff Tracy rule: Star QB out = -10.0 pts)
        home_inj_data = injuries_map.get(m["home"], {})
        away_inj_data = injuries_map.get(m["away"], {})
        home_injury_adj = home_inj_data.get("net_injury_points", 0.0)
        away_injury_adj = away_inj_data.get("net_injury_points", 0.0)
        # Weather overlay (wind, temp, dome)
        wx = weather_map.get((m["home"], m["away"]), {})
        wind = wx.get("wind", 0.0)
        wind_penalty = min(5.0, max(0.0, (wind - 14.0) * 0.35)) if not wx.get("indoor") else 0.0
        # Head-to-head projection (Aug 30 recalibration): total from combined
        # strength, margin from composite gap. Handles neutral site, elite
        # totals (~52-58), and FCS blowouts (52-0 class finals) in one model.
        h2h = project_head_to_head(
            home,
            away,
            neutral_site=bool(m.get("neutral_site")),
            home_injury_adj=home_injury_adj,
            away_injury_adj=away_injury_adj,
            wind_penalty=wind_penalty,
        )
        home_proj = h2h["home_proj"]
        away_proj = h2h["away_proj"]
        home_proj_data = {"projected_score": home_proj, "composite": h2h["home_composite"],
                          "win_probability": h2h["home_win_probability"]}
        away_proj_data = {"projected_score": away_proj, "composite": h2h["away_composite"],
                          "win_probability": h2h["away_win_probability"]}
        diff = h2h["differential"]
        # Betting market overlay — PRE-GAME ONLY. Once a game kicks off, books
        # serve live-adjusted lines that mean something completely different
        # (UVA -14/-21 in-play vs -3.5 pre-game). Suppress the line entirely
        # for started games instead of showing a number that isn't a bet.
        odds_key = (_normalize_team_name(m["home"]), _normalize_team_name(m["away"]))
        found_key = _find_odds_entry(odds_map, *odds_key, m.get("date") or "")
        market_odds = odds_map.get(found_key, {}) if found_key else {}
        game_kick = m.get("date")
        try:
            game_started = bool(game_kick) and datetime.fromisoformat(
                str(game_kick).replace("Z", "+00:00")) <= datetime.now(timezone.utc)
        except Exception:
            game_started = False
        if game_started and market_odds:
            market_odds = {}  # live game: no line, no ATS pick, no O/U rec
        # Resolve conference
        home_conf = home.get("conf") or conf_map.get(m["home"], "")
        away_conf = away.get("conf") or conf_map.get(m["away"], "")
        # Over/Under: model total vs book total
        model_total, total_vs_model, over_pick = _totals_fields(home_proj, away_proj, market_odds.get("total"))
        enriched.append({
            **m,
            "home_proj": home_proj,
            "away_proj": away_proj,
            "differential": diff,
            "home_favorite": diff > 0,
            "projected_winner": "home" if diff > 0 else ("away" if diff < 0 else None),
            "ats_pick": _ats_side(diff, market_odds.get("spread")),
            "home_composite": home_proj_data["composite"],
            "away_composite": away_proj_data["composite"],
            "home_win_prob": home_proj_data["win_probability"],
            "away_win_prob": away_proj_data["win_probability"],
            "home_sp": home.get("sp_plus", 0),
            "away_sp": away.get("sp_plus", 0),
            "market_spread": market_odds.get("spread"),
            "market_total": market_odds.get("total"),
            # Unified line fields (consistent with /api/schedule)
            # line_diff: negative = home favorite, positive = home underdog (matches spread sign)
            "line_diff": round(market_odds.get("spread"), 1) if market_odds.get("spread") is not None else None,
            "line_total": market_odds.get("total"),
            "line_source": market_odds.get("source"),
            # Compare model differential to market line (both + = home favorite)
            "line_vs_model": round(diff + (market_odds.get("spread") or 0), 1) if market_odds.get("spread") is not None else None,
            "spread_stars": 0,  # CFO enforcement: Spread stars gated OFF until ATS clears break-even
            "total_stars": 3 if (total_vs_model is not None and 4.0 <= abs(total_vs_model) < 7.0) else 0,  # CFO enforcement: 3-Star Totals only (5-star totals vetoed)
            "is_star_pick": bool(total_vs_model is not None and 4.0 <= abs(total_vs_model) < 7.0),
            # Over/under projection
            "model_total": model_total,
            "total_vs_model": total_vs_model,
            "over_pick": over_pick,
            "home_record": f"{home.get('wins',0)}-{home.get('losses',0)}",
            "away_record": f"{away.get('wins',0)}-{away.get('losses',0)}",
            "home_conf": home_conf,
            "away_conf": away_conf,
            "weather": wx,
            "wind_penalty": round(wind_penalty, 1),
            "home_injury_adj": home_injury_adj,
            "away_injury_adj": away_injury_adj,
            "home_injuries": home_inj_data.get("injuries", []),
            "away_injuries": away_inj_data.get("injuries", []),
            "home_logo_url": _LOGO_MAP.get(m["home"].lower()),
            "away_logo_url": _LOGO_MAP.get(m["away"].lower()),
        })
    # Lock in this week's SU + ATS picks (idempotent)
    _lock_picks(enriched, week)
    # FINAL SCORES overlay (Sep 8): attach completed-game results so the page can
    # sink finished games to the bottom and stamp the winner. Same CFBD finals
    # feed the grading pipeline uses; lookup is name-normalized both ways since
    # CFBD's home/away order matches ours here (both come from /games).
    try:
        finals = _fetch_final_scores()
        for m in enriched:
            key = frozenset({_norm_key_name(m["home"]), _norm_key_name(m["away"])})
            g = finals.get(key)
            if not g:
                m["final"] = None
                continue
            if _norm_key_name(g["home"]) == _norm_key_name(m["home"]):
                hs, as_ = g["home_score"], g["away_score"]
            else:
                hs, as_ = g["away_score"], g["home_score"]
            m["final"] = {
                "home_score": hs, "away_score": as_,
                "winner": m["home"] if hs > as_ else (m["away"] if as_ > hs else None),
            }
    except Exception as e:
        print(f"[Schedule] finals overlay failed: {e}")
    # D1 live write-path: model_predictions, written PRE-KICKOFF (risk register
    # D2). This is the only place per-game model output is produced, so it is
    # where those rows have to originate. Throttled to once an hour per process
    # so a page load can't become a write storm; the writer itself refuses any
    # game whose kickoff has already passed.
    try:
        _maybe_write_predictions(enriched)
    except Exception as e:  # noqa: BLE001 — never break the schedule page
        print(f"[Schedule] D1 predictions failed: {e}")
    _payload = {"week": week, "season": year, "updated": datetime.now().isoformat(),
                "matchups": enriched, "has_odds": len(odds_map) > 0, "note": note}
    _SCHEDULE_FETCH_CACHE[_key] = {"ts": time.time(), "data": _payload}
    return _payload


@app.get("/api/schedule/weeks")
def api_schedule_weeks(year: int = CFBD_YEAR):
    """Week numbers that currently have FBS games in the CFBD season list.

    The frontend builds its week dropdown from this so weeks appear as CFBD
    publishes them (and bye weeks like Week 14 don't show up)."""
    try:
        return {"year": year, "weeks": cfbd_weeks(year)}
    except Exception as e:
        print(f"[GET /api/schedule/weeks ERROR] {e}")
        raise HTTPException(502, f"Weeks fetch failed: {e}")


@app.get("/api/injuries")
def api_injuries():
    """Get all active college football injuries, tracked key players, and point deductions."""
    try:
        if ACTIVE_INJURIES_FILE.exists():
            with open(ACTIVE_INJURIES_FILE, "r") as f:
                return json.load(f)
        return {"teams": {}, "total_teams_with_injuries": 0, "total_tracked_injuries": 0}
    except Exception as e:
        print(f"[GET /api/injuries ERROR] {e}")
        return {"error": str(e), "teams": {}}


@app.post("/api/injuries/sync", dependencies=_ADMIN)
def api_injuries_sync():
    """Trigger the live injury scraper to refresh active injuries from Covers."""
    try:
        import scripts.fetch_injuries as scraper
        result = scraper.scrape_injuries()
        return {
            "status": "synced",
            "teams": result.get("total_teams_with_injuries", 0),
            "injuries": result.get("total_tracked_injuries", 0),
            "key_qbs": result.get("key_qb_injuries", 0)
        }
    except Exception as e:
        print(f"[POST /api/injuries/sync ERROR] {e}")
        raise HTTPException(500, f"Injury sync failed: {e}")


@app.post("/api/injuries/override", dependencies=_ADMIN)
def api_injuries_override(payload: dict):
    """Set or remove a manual player injury override.
    Format: {"team": "Texas", "player": "Quinn Ewers", "pos": "QB", "status": "Out", "deduction": -10.0}
    Or {"team": "Texas", "action": "clear"} to clear overrides.
    """
    team = payload.get("team")
    if not team:
        raise HTTPException(400, "Missing team name")
    data = {}
    if ACTIVE_INJURIES_FILE.exists():
        try:
            with open(ACTIVE_INJURIES_FILE, "r") as f:
                data = json.load(f)
        except Exception:
            data = {}
    teams_dict = data.setdefault("teams", {})
    team_entry = teams_dict.setdefault(
        team,
        {"team": team, "net_injury_points": 0.0, "injuries": [], "manual_overrides": []}
    )
    
    if payload.get("action") == "clear":
        team_entry["manual_overrides"] = []
    else:
        player = payload.get("player", "Key Player")
        pos = payload.get("pos", "QB")
        status = payload.get("status", "Out")
        deduction = float(payload.get("deduction", -10.0))
        if deduction > 0:
            deduction = -deduction
        override_item = {
            "player": player, "pos": pos, "status": status,
            "tier": "manual_override", "deduction": deduction,
            "updated": datetime.now().strftime("%Y-%m-%d"),
            "manual": True
        }
        team_entry["manual_overrides"] = [
            m for m in team_entry.get("manual_overrides", []) if m.get("player") != player
        ]
        team_entry["manual_overrides"].append(override_item)

    # Recalculate net injury points
    all_items = team_entry.get("injuries", []) + team_entry.get("manual_overrides", [])
    team_entry["net_injury_points"] = round(sum(i.get("deduction", 0.0) for i in all_items), 1)

    with open(ACTIVE_INJURIES_FILE, "w") as f:
        json.dump(data, f, indent=2)
    return {"status": "updated", "team": team, "net_injury_points": team_entry["net_injury_points"]}

# ── Records: Straight-Up (SU) + Against-the-Spread (ATS) tracking ──
RECORD_FILE = BASE_DIR / "data" / "record.json"


def _totals_fields(home_proj: float, away_proj: float, book_total: float | None) -> tuple:
    """Compute model total, model-vs-book gap, and over/under pick.

    Returns (model_total, total_vs_model, over_pick) where:
      model_total     = home_proj + away_proj
      total_vs_model  = model_total - book_total (positive = model likes OVER)
      over_pick       = "over" | "under" | None (None if no book total)
    """
    model_total = round(home_proj + away_proj, 1)
    if book_total is None:
        return model_total, None, None
    gap = round(model_total - book_total, 1)
    over_pick = "over" if gap > 0 else ("under" if gap < 0 else None)
    return model_total, gap, over_pick


def _ats_side(diff: float, spread: float | None) -> str | None:
    """Which side covers the spread per the model: 'home', 'away', or None.

    diff   = home_proj - away_proj (positive = home favored)
    spread = signed betting line (negative = home favorite), e.g. -35.5

    Home covers if home wins by MORE than |spread| (i.e. diff > -spread).
    If the model margin is UNDER the spread, the underdog is the ATS side.
    """
    if spread is None or diff == 0:
        return None
    cover_line = -spread  # home must win by this to cover
    if diff > cover_line:
        return "home"  # favorite covers
    if diff < cover_line:
        return "away"  # underdog covers
    return None  # push (margin == spread)


def _record_key(home: str, away: str) -> str:
    """Stable per-matchup key (home-first, lowercase)."""
    return f"{home.strip().lower()}|{away.strip().lower()}"


def _norm_key_name(name: str) -> str:
    """Normalize a team name for fuzzy matching: lowercase + strip accents.

    'San José State' -> 'san jose state', 'Texas A&M' -> 'texas a&m'.
    Used to match CFBD full school names against pick home/away names.
    """
    import unicodedata
    s = unicodedata.normalize("NFKD", (name or "").strip().lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def _load_record() -> dict:
    try:
        with open(RECORD_FILE) as f:
            return json.load(f)
    except Exception:
        return {"season": 2026, "updated": "", "picks": [], "results": []}


def _save_record(record: dict) -> None:
    record["updated"] = datetime.now().isoformat()
    try:
        RECORD_FILE.write_text(json.dumps(record, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[RECORD SAVE ERROR] {e}")


def _compute_pick(home: str, away: str, diff: float, spread: float | None,
                  model_total: float | None = None, book_total: float | None = None) -> dict:
    """Determine the projection's SU, ATS, and O/U picks for a matchup.

    diff        = home_proj - away_proj (positive = home favored)
    spread      = signed betting line (negative = home favorite), e.g. -7.5
    model_total = projected combined score
    book_total  = the over/under line
    """
    # SU pick: projected winner (higher projected score)
    if diff > 0:
        su_pick = home
    elif diff < 0:
        su_pick = away
    else:
        su_pick = None  # pick-em

    # ATS pick: does the model margin beat the spread?
    # If the model margin is under the spread, we bet the underdog against it.
    ats_pick = None
    side = _ats_side(diff, spread)
    if side == "home":
        ats_pick = home
    elif side == "away":
        ats_pick = away

    # O/U pick: model total vs book total
    over_pick = None
    if model_total is not None and book_total:
        over_pick = "over" if model_total > book_total else "under"
    return {"su_pick": su_pick, "ats_pick": ats_pick, "over_pick": over_pick}


def _lock_picks(enriched: list[dict], week: int) -> None:
    """Persist picks for matchups, re-locking ungraded picks when inputs move.

    Idempotent by matchup key. If a pick is already locked AND already graded,
    it is never touched (history is history). If it is locked but ungraded and
    the current projections/line changed the ATS or SU side, the pick is
    re-locked to the site's current best judgment — so the record always
    matches what the schedule page shows. One projection, one ATS logic.
    """
    record = _load_record()
    graded_keys = {r.get("key") for r in record.get("results", [])}
    existing = {p["key"]: p for p in record.get("picks", [])}
    changed = False
    for m in enriched:
        key = _record_key(m["home"], m["away"])
        spread = m.get("line_diff")
        diff = m.get("differential", 0)
        pk = _compute_pick(m["home"], m["away"], diff, spread,
                           model_total=m.get("model_total"), book_total=m.get("line_total"))
        prior = existing.get(key)
        if prior is None:
            record["picks"].append({
                "key": key,
                "week": week,
                "home": m["home"],
                "away": m["away"],
                "date": m.get("date"),
                "spread": spread,
                "total": m.get("line_total"),
                "over_pick": pk.get("over_pick"),
                "home_proj": m.get("home_proj"),
                "away_proj": m.get("away_proj"),
                **pk,
            })
            changed = True
        elif key not in graded_keys and spread is not None and (
            prior.get("ats_pick") != pk.get("ats_pick")
            or prior.get("su_pick") != pk.get("su_pick")
            or prior.get("spread") != spread
            or prior.get("over_pick") != pk.get("over_pick")
        ):
            # Re-lock: projections or the line moved since lock. Keep the
            # original lock time for reference, record the new values.
            # NEVER re-lock against a missing line (spread None = game started
            # or feed gap) — that would erase the bet we're grading against.
            prior.update({
                "spread": spread,
                "total": m.get("line_total"),
                "home_proj": m.get("home_proj"),
                "away_proj": m.get("away_proj"),
                **pk,
                "relocked": True,
            })
            changed = True
    if changed:
        _save_record(record)


def _grade_su(su_pick: str, home: str, away: str, home_score: int, away_score: int) -> str:
    """Grade a straight-up pick: W / L / push."""
    if home_score == away_score:
        return "push"
    winner = home if home_score > away_score else away
    return "W" if su_pick == winner else "L"


def _grade_ats(ats_pick: str, home: str, away: str, spread: float, home_score: int, away_score: int) -> str:
    """Grade an against-the-spread pick: W / L / push."""
    actual_margin = home_score - away_score
    cover_line = -spread  # home must beat this margin to cover
    if actual_margin == cover_line:
        return "push"
    home_covered = actual_margin > cover_line
    if ats_pick == home:
        return "W" if home_covered else "L"
    return "W" if not home_covered else "L"


# ── Results ingestion: pull final scores and grade locked picks ──
_FINALS_CACHE = {"data": {}, "ts": 0}
FINALS_TTL = 600  # re-fetch final scores at most every 10 min
FINALS_CACHE_FILE = BASE_DIR / "data" / "finals_cache.json"


def _fetch_final_scores(year: int = CFBD_YEAR) -> dict:
    """Fetch completed-game final scores from CFBD /games.

    Returns {frozenset({home_lower, away_lower}): {"home":, "away":,
             "home_score":, "away_score":}} for completed games only.
    """
    # Memory cache
    if _FINALS_CACHE["data"] and time.time() - _FINALS_CACHE["ts"] < FINALS_TTL:
        return _FINALS_CACHE["data"]
    # Disk cache (survives process restart on same instance)
    try:
        if FINALS_CACHE_FILE.exists():
            payload = json.loads(FINALS_CACHE_FILE.read_text(encoding="utf-8"))
            if time.time() - payload.get("ts", 0) < FINALS_TTL:
                # Rebuild frozenset keys (stored as "home|away" strings)
                finals = {}
                for k, v in payload.get("finals", {}).items():
                    finals[frozenset(k.split("|"))] = v
                _FINALS_CACHE["data"] = finals
                _FINALS_CACHE["ts"] = payload.get("ts", 0)
                return finals
    except Exception as e:
        print(f"[Finals] disk cache read failed: {e}")
    # Live fetch
    finals = {}
    try:
        games = _http_get(f"{CFBD_BASE}/games", params={"year": year}, headers=CFBD_HEADERS)
        for g in (games or []):
            if not g.get("completed") or g.get("homePoints") is None:
                continue
            h = g.get("homeTeam", "")
            a = g.get("awayTeam", "")
            if not h or not a:
                continue
            key = frozenset({_norm_key_name(h), _norm_key_name(a)})
            finals[key] = {
                "home": h, "away": a,
                "home_score": g.get("homePoints"),
                "away_score": g.get("awayPoints"),
            }
        _FINALS_CACHE["data"] = finals
        _FINALS_CACHE["ts"] = time.time()
        # Persist to disk (frozenset keys -> "home|away" strings)
        try:
            ser = {"ts": time.time(), "finals": {"|".join(sorted(k)): v for k, v in finals.items()}}
            FINALS_CACHE_FILE.write_text(json.dumps(ser), encoding="utf-8")
        except Exception as e:
            print(f"[Finals] disk cache write failed: {e}")
    except Exception as e:
        print(f"[Finals fetch failed] {e}")
    return finals


def _fetch_closing_lines_map(year: int = CFBD_YEAR) -> dict:
    """Fetch closing lines from CFBD /lines and map (home_norm, away_norm) -> (spread, total)."""
    try:
        url = f"{CFBD_BASE}/lines"
        params = {"year": year}
        data = _http_get(url, params=params, headers=CFBD_HEADERS, retries=2, base_delay=0.5)
        out = {}
        for g in (data or []):
            h = _norm_key_name(g.get("homeTeam", ""))
            a = _norm_key_name(g.get("awayTeam", ""))
            if not h or not a:
                continue
            spread = None
            total = None
            for prov in ("DraftKings", "Draft Kings", "Bovada"):
                for l in g.get("lines", []):
                    if l.get("provider") == prov:
                        if spread is None and l.get("spread") is not None:
                            spread = float(l["spread"])
                        if total is None and l.get("overUnder") is not None:
                            total = float(l["overUnder"])
                    if spread is not None and total is not None:
                        break
                if spread is not None and total is not None:
                    break
            if spread is not None or total is not None:
                out[(h, a)] = {"spread": spread, "total": total}
        return out
    except Exception as e:
        print(f"[closing lines fetch failed] {e}")
        return {}


def _ingest_results() -> int:
    """Grade any locked picks that now have final scores. Returns # newly graded."""
    record = _load_record()
    finals = _fetch_final_scores()
    if not finals:
        return 0
    closing_map = _fetch_closing_lines_map()
    t_map = _build_team_map()
    graded = {r.get("key") for r in record.get("results", [])}
    newly = 0

    # 1. Regrade existing results that were missing ATS or Total lines
    for r in record.get("results", []):
        h_norm = _norm_key_name(r.get("home", ""))
        a_norm = _norm_key_name(r.get("away", ""))
        cl = closing_map.get((h_norm, a_norm), {})
        hs = r.get("home_score")
        as_ = r.get("away_score")
        if hs is None or as_ is None:
            continue
            
        if r.get("ats_result") is None and (r.get("spread") is not None or cl.get("spread") is not None):
            spread = r.get("spread") if r.get("spread") is not None else cl.get("spread")
            ats_pick = r.get("ats_pick")
            if not ats_pick and r.get("home") in t_map and r.get("away") in t_map:
                h2h = project_head_to_head(t_map[r["home"]], t_map[r["away"]])
                ats_side = _ats_side(h2h["differential"], spread)
                ats_pick = r["home"] if ats_side == "home" else (r["away"] if ats_side == "away" else None)
            if spread is not None and ats_pick:
                r["spread"] = spread
                r["ats_pick"] = ats_pick
                r["ats_result"] = _grade_ats(ats_pick, r["home"], r["away"], spread, hs, as_)
                
        if r.get("total_result") is None and (r.get("total") is not None or cl.get("total") is not None):
            total = r.get("total") if r.get("total") is not None else cl.get("total")
            over_pick = r.get("over_pick")
            if not over_pick and r.get("home") in t_map and r.get("away") in t_map:
                h2h = project_head_to_head(t_map[r["home"]], t_map[r["away"]])
                over_pick = "over" if h2h["total"] > total else ("under" if h2h["total"] < total else None)
            if total is not None and over_pick:
                r["total"] = total
                r["over_pick"] = over_pick
                r["total_result"] = _grade_total(over_pick, total, hs, as_)

    # 2. Grade any newly completed picks
    for p in record.get("picks", []):
        if p.get("key") in graded:
            continue
        key = frozenset({_norm_key_name(p.get("home", "")), _norm_key_name(p.get("away", ""))})
        g = finals.get(key)
        if not g:
            continue
        # Orient scores relative to the pick's home/away (CFBD may flip sides)
        if _norm_key_name(g["home"]) == _norm_key_name(p.get("home", "")):
            home_score, away_score = g["home_score"], g["away_score"]
        else:
            home_score, away_score = g["away_score"], g["home_score"]
            
        cl_key = (_norm_key_name(p.get("home", "")), _norm_key_name(p.get("away", "")))
        cl = closing_map.get(cl_key, {})
        spread = p.get("spread") if p.get("spread") is not None else cl.get("spread")
        total = p.get("total") if p.get("total") is not None else cl.get("total")
        
        su_pick = p.get("su_pick")
        ats_pick = p.get("ats_pick")
        over_pick = p.get("over_pick")
        
        if (not ats_pick or not over_pick) and p.get("home") in t_map and p.get("away") in t_map:
            h2h = project_head_to_head(t_map[p["home"]], t_map[p["away"]])
            if not ats_pick and spread is not None:
                side = _ats_side(h2h["differential"], spread)
                ats_pick = p["home"] if side == "home" else (p["away"] if side == "away" else None)
            if not over_pick and total is not None:
                over_pick = "over" if h2h["total"] > total else ("under" if h2h["total"] < total else None)

        su = _grade_su(su_pick, p.get("home"), p.get("away"), home_score, away_score)
        ats = _grade_ats(ats_pick, p.get("home"), p.get("away"), spread, home_score, away_score) if (spread is not None and ats_pick) else None
        total_res = _grade_total(over_pick, total, home_score, away_score) if (total is not None and over_pick) else None
        
        record["results"].append({
            "key": p.get("key"),
            "week": p.get("week"),
            "home": p.get("home"), "away": p.get("away"),
            "home_score": home_score, "away_score": away_score,
            "su_pick": su_pick, "ats_pick": ats_pick,
            "spread": spread, "total": total,
            "over_pick": over_pick,
            "su_result": su, "ats_result": ats, "total_result": total_res,
        })
        newly += 1
    if newly or record.get("results"):
        _save_record(record)
    return newly


@app.get("/api/record")
def api_record():
    """Get the projection's straight-up (SU) and against-the-spread (ATS) records."""
    # Grade any newly-finished games before reporting (idempotent)
    try:
        _ingest_results()
    except Exception as e:
        print(f"[ingest error] {e}")
    record = _load_record()
    su = {"wins": 0, "losses": 0, "pushes": 0, "graded": 0}
    ats = {"wins": 0, "losses": 0, "pushes": 0, "graded": 0}
    total_rec = {"wins": 0, "losses": 0, "pushes": 0, "graded": 0}
    stars_totals = {"wins": 0, "losses": 0, "pushes": 0, "graded": 0}
    stars_spreads = {"wins": 0, "losses": 0, "pushes": 0, "graded": 0}
    for r in record.get("results", []):
        for bucket, field in ((su, "su_result"), (ats, "ats_result"), (total_rec, "total_result")):
            v = r.get(field)
            if v == "W":
                bucket["wins"] += 1; bucket["graded"] += 1
            elif v == "L":
                bucket["losses"] += 1; bucket["graded"] += 1
            elif v == "push":
                bucket["pushes"] += 1
        # CFO enforcement: Split totals stars vs spread stars
        tot_res = r.get("total_result")
        ats_res = r.get("ats_result")
        if tot_res in ("W", "L") and r.get("total") is not None:
            tot_diff = abs((r.get("model_total") or 50) - (r.get("total") or 50))
            if 4.0 <= tot_diff < 7.0 or r.get("total_stars") == 3:
                if tot_res == "W": stars_totals["wins"] += 1; stars_totals["graded"] += 1
                elif tot_res == "L": stars_totals["losses"] += 1; stars_totals["graded"] += 1
                elif tot_res == "push": stars_totals["pushes"] += 1
        if ats_res in ("W", "L") and r.get("spread") is not None:
            sp_diff = abs((r.get("differential") or 0) + (r.get("spread") or 0))
            if sp_diff >= 3.5 or r.get("spread_stars", 0) >= 3:
                if ats_res == "W": stars_spreads["wins"] += 1; stars_spreads["graded"] += 1
                elif ats_res == "L": stars_spreads["losses"] += 1; stars_spreads["graded"] += 1
                elif ats_res == "push": stars_spreads["pushes"] += 1
    return {
        "season": record.get("season"),
        "updated": record.get("updated"),
        "picks": record.get("picks", []),
        "results": record.get("results", []),
        "su": su,
        "ats": ats,
        "total": total_rec,
        "stars": stars_totals,
        "stars_totals": stars_totals,
        "stars_spreads": stars_spreads,
        "su_str": f"{su['wins']}-{su['losses']}" + (f"-{su['pushes']}" if su['pushes'] else ""),
        "ats_str": f"{ats['wins']}-{ats['losses']}" + (f"-{ats['pushes']}" if ats['pushes'] else ""),
        "total_str": f"{total_rec['wins']}-{total_rec['losses']}" + (f"-{total_rec['pushes']}" if total_rec['pushes'] else ""),
        "stars_str": f"{stars_totals['wins']}-{stars_totals['losses']}" + (f"-{stars_totals['pushes']}" if stars_totals['pushes'] else ""),
        "stars_totals_str": f"{stars_totals['wins']}-{stars_totals['losses']}" + (f"-{stars_totals['pushes']}" if stars_totals['pushes'] else ""),
        "stars_spreads_str": f"{stars_spreads['wins']}-{stars_spreads['losses']}" + (f"-{stars_spreads['pushes']}" if stars_spreads['pushes'] else ""),
    }


@app.post("/api/record/ingest", dependencies=_ADMIN)
def api_record_ingest():
    """Force a results-ingestion run (pull final scores + grade picks)."""
    try:
        # Bust the finals cache so we always re-pull on manual trigger
        _FINALS_CACHE["data"] = {}
        _FINALS_CACHE["ts"] = 0
        n = _ingest_results()
        rec = _load_record()
        return {"graded": n, "total_results": len(rec.get("results", []))}
    except Exception as e:
        print(f"[ingest error] {e}")
        return {"error": str(e), "graded": 0}


@app.post("/api/record/repair-ats", dependencies=_ADMIN)
def api_record_repair_ats(spread: float, ats_pick: str, key: str):
    """One-off repair: restore a pick's spread/ats_pick erased by the Aug 29
    re-lock bug (re-lock fired against a suppressed live-game line, writing
    None over the legitimate pre-kickoff lock), then re-grade its result.

    Only touches picks whose result has ats_result=None (ungraded ATS)."""
    try:
        record = _load_record()
        fixed = []
        for p in record.get("picks", []):
            if p.get("key") == key and p.get("spread") is None:
                p["spread"] = spread
                p["ats_pick"] = ats_pick
                fixed.append(p["key"])
        reground = 0
        for r in record.get("results", []):
            if r.get("key") == key and r.get("ats_result") is None:
                match = next((p for p in record["picks"] if p.get("key") == key), None)
                if match and match.get("spread") is not None and match.get("ats_pick"):
                    r["spread"] = match["spread"]
                    r["ats_pick"] = match["ats_pick"]
                    r["ats_result"] = _grade_ats(
                        match["ats_pick"], match["home"], match["away"],
                        match["spread"], r["home_score"], r["away_score"])
                    reground += 1
        if fixed or reground:
            _save_record(record)
        return {"fixed_picks": fixed, "regraded": reground}
    except Exception as e:
        print(f"[repair error] {e}")
        return {"error": str(e)}


# ── Best Bets: separate tracker for high-confidence value plays ──
BEST_BETS_FILE = BASE_DIR / "data" / "best_bets.json"


def _load_best_bets() -> dict:
    try:
        with open(BEST_BETS_FILE) as f:
            return json.load(f)
    except Exception:
        return {"season": 2026, "updated": "", "picks": [], "results": []}


def _save_best_bets(bb: dict) -> None:
    bb["updated"] = datetime.now().isoformat()
    try:
        BEST_BETS_FILE.write_text(json.dumps(bb, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[BEST BETS SAVE ERROR] {e}")


def _lock_best_bets(plays: list[dict], week: int) -> None:
    """Persist best-bets picks (spread + total sides) for matchups not yet locked."""
    bb = _load_best_bets()
    existing = {p["key"] for p in bb.get("picks", [])}
    changed = False
    for play in plays:
        key = _record_key(play.get("home", ""), play.get("away", ""))
        if key in existing:
            continue
        # Only lock plays that actually have a pick (spread or total)
        if play.get("spread_pick") is None and play.get("over_pick") is None:
            continue
        bb["picks"].append({
            "key": key,
            "week": week,
            "home": play.get("home"),
            "away": play.get("away"),
            "date": play.get("date"),
            "spread": play.get("spread"),
            "spread_pick": play.get("spread_pick"),
            "spread_edge": play.get("spread_edge"),
            "total": play.get("total"),
            "over_pick": play.get("over_pick"),
            "total_edge": play.get("total_edge"),
            "model_total": play.get("model_total"),
        })
        changed = True
    if changed:
        _save_best_bets(bb)


def _grade_total(over_pick: str, book_total: float, home_score: int, away_score: int) -> str:
    """Grade an over/under pick: W / L / push."""
    actual_total = home_score + away_score
    if actual_total == book_total:
        return "push"
    if over_pick == "over":
        return "W" if actual_total > book_total else "L"
    return "W" if actual_total < book_total else "L"


def _ingest_best_bets() -> int:
    """Grade any locked best-bets picks that now have final scores."""
    bb = _load_best_bets()
    finals = _fetch_final_scores()
    if not finals:
        return 0
    graded = {r.get("key") for r in bb.get("results", [])}
    newly = 0
    for p in bb.get("picks", []):
        if p.get("key") in graded:
            continue
        key = frozenset({_norm_key_name(p.get("home", "")), _norm_key_name(p.get("away", ""))})
        g = finals.get(key)
        if not g:
            continue
        if _norm_key_name(g["home"]) == _norm_key_name(p.get("home", "")):
            home_score, away_score = g["home_score"], g["away_score"]
        else:
            home_score, away_score = g["away_score"], g["home_score"]
        # Grade spread pick
        spread_result = None
        if p.get("spread") is not None and p.get("spread_pick") is not None:
            spread_result = _grade_ats(p.get("spread_pick"), p.get("home"), p.get("away"),
                                       p["spread"], home_score, away_score)
        # Grade total pick
        total_result = None
        if p.get("total") is not None and p.get("over_pick") is not None:
            total_result = _grade_total(p.get("over_pick"), p["total"], home_score, away_score)
        bb["results"].append({
            "key": p.get("key"),
            "week": p.get("week"),
            "home": p.get("home"), "away": p.get("away"),
            "home_score": home_score, "away_score": away_score,
            "spread_pick": p.get("spread_pick"), "over_pick": p.get("over_pick"),
            "spread_result": spread_result,
            "total_result": total_result,
        })
        newly += 1
    if newly:
        _save_best_bets(bb)
    return newly


@app.get("/api/best-bets/record")
def api_best_bets_record():
    """Get the best-bets tracker record (spread picks + over/under picks)."""
    try:
        _ingest_best_bets()
    except Exception as e:
        print(f"[best-bets ingest error] {e}")
    bb = _load_best_bets()
    spread = {"wins": 0, "losses": 0, "pushes": 0, "graded": 0}
    total = {"wins": 0, "losses": 0, "pushes": 0, "graded": 0}
    for r in bb.get("results", []):
        for bucket, field in ((spread, "spread_result"), (total, "total_result")):
            v = r.get(field)
            if v == "W":
                bucket["wins"] += 1; bucket["graded"] += 1
            elif v == "L":
                bucket["losses"] += 1; bucket["graded"] += 1
            elif v == "push":
                bucket["pushes"] += 1
    return {
        "season": bb.get("season"),
        "updated": bb.get("updated"),
        "picks": bb.get("picks", []),
        "results": bb.get("results", []),
        "spread": spread,
        "total": total,
        "spread_str": f"{spread['wins']}-{spread['losses']}" + (f"-{spread['pushes']}" if spread['pushes'] else ""),
        "total_str": f"{total['wins']}-{total['losses']}" + (f"-{total['pushes']}" if total['pushes'] else ""),
    }


@app.post("/api/refresh", dependencies=_ADMIN)
def api_refresh():
    """Manually trigger a full refresh (lines + final scores + grading)."""
    try:
        return refresh_all()
    except Exception as e:
        print(f"[refresh error] {e}")
        return {"error": str(e)}


@app.get("/api/line-movements")
def api_line_movements(hours: int = 72):
    """Recent line movements from the rolling event log (default last 72h)."""
    try:
        hours = max(1, min(hours, 24 * 7))
        cutoff = time.time() - hours * 3600
        log = [m for m in _load_movement_log() if m.get("ts", 0) >= cutoff]
        log.sort(key=lambda m: m.get("ts", 0), reverse=True)
        return {"movements": log, "count": len(log)}
    except Exception as e:
        print(f"[line-movements error] {e}")
        return {"error": str(e), "movements": []}


@app.get("/api/best-bets")
def api_best_bets():
    """Rank upcoming games by model-vs-market divergence (spread + total edge).

    Returns the top value plays where the model disagrees most with the books,
    for both the spread and the over/under. Each pick gets a confidence (1-3
    stars) based on how far the line diverges from the model.
    """
    try:
        # Reuse the full fetch endpoint's enrichment logic
        sched = api_schedule_fetch(week=1)
        matchups = sched.get("matchups", [])
        team_map = _build_team_map()
        # FBS-only filter: drop matchups where a team has no SP+ data
        # (FCS teams like Norfolk State/Fordham/Bryant default to sp_plus=0
        # and a flat ~43 composite, producing meaningless "value" signals).
        # Real FBS teams always have a non-zero SP+ rating.
        def _has_data(name):
            td = team_map.get(name, {})
            sp = td.get("sp_plus")
            return sp is not None and sp != 0
        plays = []
        for m in matchups:
            if not _has_data(m.get("home")) or not _has_data(m.get("away")):
                continue
            spread_edge = abs(m.get("line_vs_model")) if m.get("line_vs_model") is not None else None
            total_edge = abs(m.get("total_vs_model")) if m.get("total_vs_model") is not None else None
            # Confidence from edge magnitude (points)
            def _stars(edge):
                if edge is None:
                    return 0
                if edge >= 10:
                    return 3
                if edge >= 5:
                    return 2
                return 1
            plays.append({
                "home": m.get("home"),
                "away": m.get("away"),
                "date": m.get("date"),
                "home_logo_url": m.get("home_logo_url"),
                "away_logo_url": m.get("away_logo_url"),
                # Spread side
                "spread": m.get("line_diff"),
                "spread_edge": spread_edge,
                "spread_stars": _stars(spread_edge),
                "spread_pick": m.get("ats_pick"),
                # Total side
                "total": m.get("line_total"),
                "model_total": m.get("model_total"),
                "total_edge": total_edge,
                "total_stars": _stars(total_edge),
                "over_pick": m.get("over_pick"),
            })
        # Rank by combined edge (max of spread/total edge), only games with a line
        ranked = [p for p in plays if p["spread_edge"] is not None or p["total_edge"] is not None]
        ranked.sort(key=lambda p: max(p["spread_edge"] or 0, p["total_edge"] or 0), reverse=True)
        top = ranked[:10]
        # Lock the top plays into the best-bets tracker (idempotent)
        _lock_best_bets(top, sched.get("week", 1))
        return {"best_bets": top, "count": len(ranked)}
    except Exception as e:
        print(f"[GET /api/best-bets ERROR] {e}")
        return {"error": str(e), "best_bets": []}


# ── Season Win-Total Projections (over/under team wins) ────────────────
# Runs EVERY 2026 game in a team's schedule through the multi-factor model,
# converts each projected-score differential into a head-to-head win
# probability, and sums them. The sum is the expected total wins — the fair
# number to bet against a sportsbook's over/under on that team's season wins.
# Schedule strength matters: a team with 12 games vs FBS opponents gets its
# projection from real matchups, not an average.

_WIN_TOTALS_CACHE = {}          # {year: {"ts":..., "teams":[...]}}
WIN_TOTALS_TTL = 3600           # recompute at most hourly (schedule is static)
_H2H_SCALE = 3.0                # logistic scale, calibrated Aug 2026 vs 138 sportsbook win-total O/U lines (S=6.0 compressed elite teams ~2 wins low)


def _h2h_win_prob(home_score: float, away_score: float) -> float:
    """Head-to-head win probability from projected scores (logistic).

    diff = home - away projected points. A +3 pt edge ≈ 73% to win; symmetric
    around 50%. Scale calibrated against sportsbook season-win O/U lines so
    elite teams project near their book totals instead of compressing toward
    the middle. This is a game-level model, so it already includes whatever
    home-field adjustment was baked into the projected scores — we do NOT add
    HFA again here."""
    diff = home_score - away_score
    return 1.0 / (1.0 + math.exp(-diff / _H2H_SCALE))


# ── CFBD season games cache ──
# The full-season game list is static within a week and shared by the Schedule
# page, Win Totals, and the weekly sync. Cache in memory (30 min) + persist to
# disk so container restarts reuse the last pull instead of re-fetching.
_CFBD_GAMES_CACHE = {}   # {year: {"ts": float, "games": [dict]}}
CFBD_GAMES_TTL = 1800    # 30 minutes — schedule is static within a week
_CFBD_GAMES_FILE = BASE_DIR / "data" / "cfbd_season_games.json"


def _cfbd_season_games(year: int) -> list[dict]:
    """Full-season game list from CFBD (one call, all weeks).

    Cached in memory for CFBD_GAMES_TTL seconds and persisted to disk. On a
    fresh empty result (API hiccup), falls back to the last good disk copy."""
    cached = _CFBD_GAMES_CACHE.get(year)
    if cached and time.time() - cached["ts"] < CFBD_GAMES_TTL:
        return cached["games"]
    games = [g for g in _cfbd_get("games", year) if isinstance(g, dict)]
    if not games and _CFBD_GAMES_FILE.exists():
        try:
            payload = json.loads(_CFBD_GAMES_FILE.read_text(encoding="utf-8"))
            # Disk copy is a fallback only — allow up to 4x the TTL staleness.
            if time.time() - payload.get("ts", 0) < CFBD_GAMES_TTL * 4:
                games = [g for g in payload.get("games", []) if isinstance(g, dict)]
        except Exception:
            pass
    _CFBD_GAMES_CACHE[year] = {"ts": time.time(), "games": games}
    try:
        _CFBD_GAMES_FILE.write_text(
            json.dumps({"ts": time.time(), "games": games}), encoding="utf-8")
    except Exception:
        pass
    return games


def compute_win_totals(year: int = CFBD_YEAR) -> dict:
    """Project each FBS team's total season wins from its full schedule.

    Returns {"year":..., "teams":[{name, conf, games, exp_wins, proj_record,
    home_games, away_games, neutral_games, ...}], "generated":...}.
    Cached in memory for WIN_TOTALS_TTL seconds."""
    cached = _WIN_TOTALS_CACHE.get(year)
    if cached and time.time() - cached["ts"] < WIN_TOTALS_TTL:
        return cached

    games = _cfbd_season_games(year)
    team_map = _build_team_map()          # name -> full analytics dict
    known = set(team_map.keys())

    # FBS-only: this is a betting tool, so report the 138 FBS teams. CFBD tags
    # each side's classification on every game; union them to get the FBS set.
    fbs_names = set()
    for g in games:
        if (g.get("homeClassification") or "").upper().startswith("FBS"):
            fbs_names.add(g["homeTeam"])
        if (g.get("awayClassification") or "").upper().startswith("FBS"):
            fbs_names.add(g["awayTeam"])

    # Per-team accumulators
    acc = defaultdict(lambda: {
        "games": 0, "home": 0, "away": 0, "neutral": 0,
        "exp_wins": 0.0, "proj_pts_for": 0.0, "proj_pts_against": 0.0,
        "schedule": [],
        "played_games": 0, "actual_wins": 0, "actual_losses": 0, "actual_ties": 0,
    })
    games_analyzed = 0

    for g in games:
        home_name = g.get("homeTeam") or ""
        away_name = g.get("awayTeam") or ""
        if not home_name or not away_name:
            continue
        # Only project games where BOTH teams are in our analytics set.
        if home_name not in known or away_name not in known:
            continue
        # FBS-only accumulation: skip pure non-FBS (FCS/D2) matchups entirely;
        # an FBS-vs-non-FBS game still counts for the FBS side.
        h_fbs = home_name in fbs_names
        a_fbs = away_name in fbs_names
        if not (h_fbs or a_fbs):
            continue
        games_analyzed += 1

        neutral = bool(g.get("neutralSite"))
        home_td = team_map[home_name]
        away_td = team_map[away_name]

        # Projected scores. On a neutral site, zero out the HFA tilt so the
        # game is decided purely by strength (the model bakes in +2.5/-1.5).
        if neutral:
            h_proj = project_score_multi_factor(home_td, is_home=True)["projected_score"]
            a_proj = project_score_multi_factor(away_td, is_home=False)["projected_score"]
            # Re-center: remove the HFA asymmetry by averaging both directions.
            h_proj2 = project_score_multi_factor(home_td, is_home=False)["projected_score"]
            a_proj2 = project_score_multi_factor(away_td, is_home=True)["projected_score"]
            home_score = (h_proj + h_proj2) / 2.0
            away_score = (a_proj + a_proj2) / 2.0
        else:
            home_score = project_score_multi_factor(home_td, is_home=True)["projected_score"]
            away_score = project_score_multi_factor(away_td, is_home=False)["projected_score"]

        p_home = _h2h_win_prob(home_score, away_score)

        # Per-game schedule entries (for the expandable "which games do they lose" view).
        week = g.get("week") or 0
        start_date = (g.get("startDate") or "")[:10]
        h_entry = {
            "week": week, "date": start_date, "opp": away_name, "at_home": not neutral,
            "neutral": neutral, "proj_for": round(home_score, 1),
            "proj_against": round(away_score, 1), "p_win": p_home,
        }
        a_entry = {
            "week": week, "date": start_date, "opp": home_name, "at_home": False,
            "neutral": neutral, "proj_for": round(away_score, 1),
            "proj_against": round(home_score, 1), "p_win": 1.0 - p_home,
        }

        # Completed games: use the ACTUAL result (CFBD /games carries final
        # points). The model's pre-game probability is kept for reference, but
        # classification and win accumulation come from the real outcome — so
        # mid-season exp_wins = actual wins + projected remainder.
        played = bool(g.get("completed")) and g.get("homePoints") is not None \
            and g.get("awayPoints") is not None
        if played:
            h_pts, a_pts = int(g["homePoints"]), int(g["awayPoints"])
            h_entry.update(played=True, actual_for=h_pts, actual_against=a_pts)
            a_entry.update(played=True, actual_for=a_pts, actual_against=h_pts)
            if h_pts > a_pts:
                h_entry["cls"] = "W"; a_entry["cls"] = "L"
            elif h_pts < a_pts:
                h_entry["cls"] = "L"; a_entry["cls"] = "W"
            else:
                h_entry["cls"] = "T"; a_entry["cls"] = "T"

        # Home team accumulates (FBS only — a non-FBS opponent still supplies the
        # matchup projection but never gets its own row).
        if h_fbs:
            ah = acc[home_name]
            ah["games"] += 1
            if played:
                # Actual result drives the total; model prob is reference only.
                ah["exp_wins"] += {"W": 1.0, "L": 0.0, "T": 0.5}[h_entry["cls"]]
                ah["played_games"] += 1
                if h_entry["cls"] == "W":
                    ah["actual_wins"] += 1
                elif h_entry["cls"] == "L":
                    ah["actual_losses"] += 1
                else:
                    ah["actual_ties"] += 1
            else:
                ah["exp_wins"] += p_home
            ah["proj_pts_for"] += home_score
            ah["proj_pts_against"] += away_score
            ah["schedule"].append(h_entry)
            if neutral:
                ah["neutral"] += 1
            else:
                ah["home"] += 1

        # Away team accumulates (mirror)
        if a_fbs:
            aa = acc[away_name]
            aa["games"] += 1
            if played:
                aa["exp_wins"] += {"W": 1.0, "L": 0.0, "T": 0.5}[a_entry["cls"]]
                aa["played_games"] += 1
                if a_entry["cls"] == "W":
                    aa["actual_wins"] += 1
                elif a_entry["cls"] == "L":
                    aa["actual_losses"] += 1
                else:
                    aa["actual_ties"] += 1
            else:
                aa["exp_wins"] += (1.0 - p_home)
            aa["proj_pts_for"] += away_score
            aa["proj_pts_against"] += home_score
            aa["schedule"].append(a_entry)
            if neutral:
                aa["neutral"] += 1
            else:
                aa["away"] += 1

    # Build the team list (FBS only, with a conference + logo for display)
    teams_out = []
    for name, a in acc.items():
        td = team_map.get(name, {})
        if a["games"] == 0:
            continue
        exp_wins = round(a["exp_wins"], 2)
        # Projected record: wins rounded to nearest .5 (betting-friendly),
        # losses = games - wins.
        proj_w = round(exp_wins * 2) / 2.0
        proj_l = a["games"] - proj_w

        # Per-game classification for the expandable schedule view:
        # played games already carry their ACTUAL cls (W/L/T); unplayed games
        # use the model: W = projected win (p>=75%), L = loss (p<=25%), T = toss-up.
        sched = []
        for e in sorted(a["schedule"], key=lambda x: (x["week"] or 0, x["date"])):
            if "cls" not in e:
                p = e["p_win"]
                e["cls"] = "W" if p >= 0.75 else ("L" if p <= 0.25 else "T")
            sched.append(e)
        n_w = sum(1 for s in sched if s["cls"] == "W")
        n_t = sum(1 for s in sched if s["cls"] == "T")
        n_l = sum(1 for s in sched if s["cls"] == "L")

        # Actual record from completed games (mid-season). Ties count as 0-0-1.
        actual_record = f"{a['actual_wins']}–{a['actual_losses']}" + \
            (f"–{a['actual_ties']}" if a["actual_ties"] else "")

        teams_out.append({
            "name": name,
            "conf": td.get("conf") or "",
            "logo_url": td.get("logo_url"),
            "composite": round(project_score_multi_factor(td)["composite"], 1),
            "games": a["games"],
            "home_games": a["home"],
            "away_games": a["away"],
            "neutral_games": a["neutral"],
            "played_games": a["played_games"],
            "actual_record": actual_record,
            "exp_wins": exp_wins,
            "proj_record": f"{proj_w:g}–{proj_l:g}",
            "proj_ppg_for": round(a["proj_pts_for"] / a["games"], 1),
            "proj_ppg_against": round(a["proj_pts_against"] / a["games"], 1),
            "wtl": f"{n_w}W · {n_t}T · {n_l}L",
            "schedule": sched,
        })

    # Sort by expected wins descending (the natural over/under ranking)
    teams_out.sort(key=lambda t: t["exp_wins"], reverse=True)
    for i, t in enumerate(teams_out, 1):
        t["rank"] = i

    result = {
        "year": year,
        "generated": datetime.now().isoformat(),
        "games_analyzed": games_analyzed,
        "teams": teams_out,
    }
    _WIN_TOTALS_CACHE[year] = {"ts": time.time(), **result}
    return result


@app.get("/api/win-totals")
def api_win_totals(year: int | None = None):
    """Projected total season wins per FBS team (over/under reference)."""
    yr = year or CFBD_YEAR
    try:
        data = compute_win_totals(yr)
        return data
    except Exception as e:
        print(f"[GET /api/win-totals ERROR] {e}")
        raise HTTPException(status_code=502, detail=f"Win-total projection failed: {e}")


# ── Serve frontend ──
# No-cache headers so the browser always pulls the fresh page (prevents
# stale cached HTML/JS from masking code changes).
_NO_CACHE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}

@app.get("/")
def index():
    return FileResponse(BASE_DIR / "index.html", headers=_NO_CACHE)

@app.get("/analytics")
def analytics_page():
    return FileResponse(BASE_DIR / "analytics.html", headers=_NO_CACHE)

@app.get("/schedule")
def schedule_page():
    return FileResponse(BASE_DIR / "schedule.html", headers=_NO_CACHE)

@app.get("/win-totals")
def win_totals_page():
    return FileResponse(BASE_DIR / "win_totals.html", headers=_NO_CACHE)


# Start the background auto-refresh scheduler. Runs whether the app is started
# via `python app.py` or `uvicorn app:app` (both import this module once).
# The daemon thread sleeps first, so it never blocks startup/readiness.
_start_scheduler()


# ── Main ──
if __name__ == "__main__":
    # NOTE: No network pre-fetching here. In production (incl. Cloudflare
    # Containers) we must not make live API calls during startup — the
    # container's readiness probe (pingEndpoint) would time out waiting for
    # those calls. Data is loaded lazily on first request and cached to disk.
    # uvicorn reload=True is also disabled for container images (the reloader
    # spawns a subprocess that confuses health checks).
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=8003,
        reload=os.environ.get("APP_RELOAD", "0") == "1",  # default off in containers
        log_level="info",
    )