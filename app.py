"""Skeez CFB Rankings — FastAPI backend with live data feed."""

import json
import os
import time
import random
from pathlib import Path
from datetime import datetime

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
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

class RankingsResponse(BaseModel):
    week: str
    season: int
    updated: str
    teams: list[Team]

# ── App ──
app = FastAPI(title="Skeez CFB Rankings API", version="1.0.0")

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
def _snapshot_lines(odds_map: dict) -> list[dict]:
    """Detect line movement vs the previous snapshot and save a new one.

    Returns a list of moved lines: {home, away, market, old, new, delta}.
    """
    prev = {}
    try:
        if LINE_HISTORY_FILE.exists():
            raw = json.loads(LINE_HISTORY_FILE.read_text(encoding="utf-8")).get("lines", {})
            prev = {tuple(k.split("|")): v for k, v in raw.items()}
    except Exception:
        prev = {}
    movements = []
    current = {}
    for (home, away), v in odds_map.items():
        key = f"{home}|{away}"
        current[key] = {"spread": v.get("spread"), "total": v.get("total"), "book": v.get("book_title") or v.get("book")}
        old = prev.get((home, away))
        if not old:
            continue
        for market in ("spread", "total"):
            new_v = v.get(market)
            old_v = old.get(market)
            if new_v is not None and old_v is not None and new_v != old_v:
                movements.append({
                    "home": home, "away": away, "market": market,
                    "old": old_v, "new": new_v, "delta": round(new_v - old_v, 1),
                })
    # Persist new snapshot (keep only latest; history of deltas is enough for CLV)
    try:
        LINE_HISTORY_FILE.write_text(
            json.dumps({"ts": time.time(), "lines": current}, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[Lines] history write failed: {e}")
    return movements


def refresh_all() -> dict:
    """Force re-pull of live odds + final scores, then grade records.

    Used by the background scheduler and the manual /api/refresh endpoint.
    Returns a summary of what changed.
    """
    result = {"odds_refreshed": False, "graded": 0, "best_bets_graded": 0, "line_movements": []}
    try:
        # Bust the in-memory + disk odds caches so we re-fetch live lines
        _odds_cache["data"] = {}
        _odds_cache["ts"] = 0
        odds_map = _fetch_odds_map()
        result["odds_refreshed"] = bool(odds_map)
        result["line_movements"] = _snapshot_lines(odds_map)
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
    result["ts"] = datetime.now().isoformat()
    return result


def _start_scheduler() -> None:
    """Background daemon thread: periodically refresh lines + grade results.

    Safe for containers — no network calls at startup; the thread sleeps first.
    """
    if REFRESH_INTERVAL_SECONDS <= 0:
        return
    import threading

    def _loop():
        # Sleep first so we never block the readiness probe / cold start.
        time.sleep(60)
        while True:
            try:
                summary = refresh_all()
                if summary["graded"] or summary["line_movements"]:
                    print(f"[scheduler] refresh: {summary}")
            except Exception as e:
                print(f"[scheduler] refresh error: {e}")
            time.sleep(REFRESH_INTERVAL_SECONDS)

    t = threading.Thread(target=_loop, daemon=True, name="cfb-refresh")
    t.start()

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

def fetch_espn_poll() -> list[dict] | None:
    """
    Fetch the AP Top 25 from ESPN's public rankings API.
    Returns parsed team list or None on failure.

    Endpoint: /sports/football/college-football/rankings returns ALL polls
    (AP, AFCA Coaches, FCS) for the current week; we keep only "AP Top 25".
    The old /standings/ap-top-25 endpoint was retired by ESPN (404).
    """
    try:
        url = ("https://site.api.espn.com/apis/site/v2/sports/football/"
               "college-football/rankings")
        data = _http_get(url, retries=3, base_delay=1.0)
        if not data or not isinstance(data, dict):
            return None

        polls = [p for p in data.get("rankings", [])
                 if p.get("name") == "AP Top 25"]
        if not polls:
            return None

        teams = []
        for rk in polls[0].get("ranks", []):
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
        return teams or None

    except Exception as e:
        print(f"[ESPN fetch failed] {e}")
        return None


# AP poll changes weekly — cache an hour so page loads don't hammer ESPN.
_ap_poll_cache: dict = {}
AP_POLL_TTL = 3600


def _ap_rank_map() -> dict[str, int]:
    """{team name (lowercase): AP rank} for the current AP Top 25.

    Empty dict on any failure — callers must treat a missing key as
    'not ranked' and never let an ESPN outage break the rankings page."""
    cached = _cache_get(_ap_poll_cache, AP_POLL_TTL)
    if cached:
        return cached
    poll = fetch_espn_poll()
    m = {t["name"].lower(): t["rank"] for t in (poll or [])}
    if m:
        _cache_set(_ap_poll_cache, m)
    return m

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

    # AP Top 25 cross-reference: each team's current AP poll rank (or None if
    # not ranked). ESPN outage => empty map => everyone shows "—", never an error.
    ap_map = _ap_rank_map()
    for t in teams:
        t["ap_rank"] = ap_map.get(t.get("name", "").lower())

    result = {
        "week": datetime.now().strftime("%B %d, %Y"),
        "season": datetime.now().year,
        "updated": datetime.now().isoformat(),
        "teams": teams,
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

@app.post("/api/rankings/refresh")
def api_refresh():
    """Force-refresh the ESPN data feed, computing composites."""
    _rankings_cache.clear()
    live = fetch_espn_poll()
    if live:
        teams = [t for t in live if t.get("rank", 0) > 0]
        team_map = _build_team_map()
        for team in teams:
            name = team["name"]
            td = team_map.get(name, {})
            proj = project_score_multi_factor(td, is_home=True)
            team["composite"] = proj["composite"]
            logo = _LOGO_MAP.get(name.lower())
            if logo:
                team["logo_url"] = logo
        teams = sorted(teams, key=lambda t: t.get("composite", 0), reverse=True)
        result = {
            "week": datetime.now().strftime("%B %d, %Y"),
            "season": datetime.now().year,
            "updated": datetime.now().isoformat(),
            "teams": teams,
        }
        _cache_set(_rankings_cache, result)
        return {"status": "refreshed", "source": "espn", "teams": len(teams)}
    else:
        return {"status": "fallback", "source": "local", "note": "ESPN fetch failed, using local data"}

@app.get("/api/health")
def api_health():
    """Health check."""
    return {"status": "ok", "teams": len(load_local()), "cache_ttl": CACHE_TTL}

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
        team["cpi_contribution"] = proj["cpi_contribution"]
        team["elo_contribution"] = proj["elo_contribution"]
        team["rec_contribution"] = proj["rec_contribution"]
        team["epa_contribution"] = proj["epa_contribution"]
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
    result = {
        "week": datetime.now().strftime("%B %d, %Y"),
        "season": CFBD_YEAR,
        "updated": datetime.now().isoformat(),
        "teams": teams,
        "source": "cfbd",
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

def _cfbd_get(endpoint: str, year: int = CFBD_YEAR) -> list:
    """Fetch JSON from CFBD API with auth, retry with exponential backoff."""
    url = f"{CFBD_BASE}/{endpoint}"
    params = {"year": year}
    data = _http_get(url, params=params, headers=CFBD_HEADERS)
    if data is None:
        return []
    return data if isinstance(data, list) else [data]


def _http_get(url: str, params: dict | None = None, headers: dict | None = None,
              retries: int = 3, base_delay: float = 1.0) -> list:
    """GET with exponential backoff retry. Returns parsed JSON list, or [] on failure."""
    last_err = None
    for attempt in range(retries):
        try:
            resp = _HTTP_CLIENT.get(url, headers=headers, params=params)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                delay = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
                time.sleep(delay)
    print(f"[_http_get] {url} failed after {retries} retries: {last_err}")
    return None

def _cfbd_teams() -> dict:
    """Fetch all FBS teams with logos, conferences, colors."""
    data = _cfbd_get("teams", CFBD_YEAR)
    if not data:
        data = _cfbd_get("teams", CFBD_YEAR_FALLBACK)
    return {t["school"]: t for t in data}

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

def _cfbd_srs() -> dict:
    """Fetch SRS ratings, falling back to 2025 if 2026 empty."""
    data = _cfbd_get("ratings/srs", CFBD_YEAR)
    if not data:
        data = _cfbd_get("ratings/srs", CFBD_YEAR_FALLBACK)
    return {t["team"]: t for t in data}

def _cfbd_elo() -> dict:
    """Fetch Elo ratings, falling back to 2025 if 2026 empty."""
    data = _cfbd_get("ratings/elo", CFBD_YEAR)
    if not data:
        data = _cfbd_get("ratings/elo", CFBD_YEAR_FALLBACK)
    return {t["team"]: t for t in data}

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
            out[team] = {
                # Real points per game from game scores (fall back to stats-based if no games found)
                "off_ppg": round(pf.get(team, 0) / ng.get(team, 1), 1) if team in ng else None,
                "def_ppg": round(pa.get(team, 0) / ng.get(team, 1), 1) if team in ng else None,
                "off_ypp": round(s.get("totalYards", 0) / plays, 2) if plays else None,
                "def_ypp": round(s.get("totalYardsOpponent", 0) / opp_plays, 2) if opp_plays else None,
                "off_3rd": round(s.get("thirdDownConversions", 0) / max(1, s.get("thirdDowns", 1)), 3),
                "def_3rd": round(s.get("thirdDownConversionsOpponent", 0) / max(1, s.get("thirdDownsOpponent", 1)), 3),
                "turnover_margin": round(to / games_played, 2),
            }
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
                "pts_per_poss": round(pts / max(1, scoring), 2),
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
                "def_pts_per_poss": round(d_pts / max(1, d_scoring), 2),
                "def_td_rate": round(d_tds / d_total * 100, 1),
                "def_fg_rate": round(d_fgs / max(1, d_scoring) * 100, 1),
                "def_turnover_created": round(d_tos / d_total * 100, 1),
            })
    return out


def _the_odds_fetch() -> list[dict]:
    """Fetch live NCAAF spreads and totals from The Odds API."""
    url = f"{THE_ODDS_BASE}/sports/americanfootball_ncaaf/odds"
    params = {
        "apiKey": THE_ODDS_API_KEY,
        "regions": "us",
        "markets": "spreads,totals",
        "oddsFormat": "decimal",
    }
    data = _http_get(url, params=params, retries=3, base_delay=1.0)
    return data if isinstance(data, list) else (data.get("events", []) if data else [])
def _cfbd_lines() -> list:
    """Fetch betting lines from CFBD API."""
    try:
        data = _cfbd_get("lines", CFBD_YEAR)
        return data
    except Exception as e:
        print(f"[CFBD lines fetch failed] {e}")
        return []


def _propline_fetch() -> list[dict]:
    """Fetch NCAAF spreads & totals from PropLine (Bovada via the-odds-compatible API).
    Uses the bulk /odds endpoint to discover events (1 call), then fetches detailed
    markets per event concurrently. Returns a list of dicts in the same shape as
    _the_odds_fetch() so it can be merged into the same odds_map.
    """
    if not PROPLINE_KEY:  # Use module-level key (set from .env at startup)
        return []
    try:
        base = "https://api.prop-line.com/v1/sports/football_ncaaf"
        # 1. Bulk fetch all events with basic odds (1 API call, with retry)
        bulk = _http_get(f"{base}/odds", params={"apiKey": PROPLINE_KEY}, retries=3, base_delay=1.0)
        if not bulk:
            return []
        events = bulk if isinstance(bulk, list) else bulk.get("events", [])
        # 2. Concurrent per-event market fetch (113 calls done in parallel, ~5s vs ~20s sequential)
        def _parse_event(ev):
            home = ev.get("home_team", "")
            away = ev.get("away_team", "")
            if not home or not away:
                return None
            eid = ev.get("event_id") or ev.get("id")
            if not eid:
                return None
            od = _http_get(f"{base}/events/{eid}/odds", params={"apiKey": PROPLINE_KEY},
                           retries=2, base_delay=0.5)
            if not od:
                return None
            # Pass through ALL bookmakers so _build_odds_map can pick the best
            # book and record its name (DraftKings/FanDuel/etc.) for display.
            return {
                "home_team": home,
                "away_team": away,
                "bookmakers": od.get("bookmakers", []),
            }
        # Concurrent fetch for all events
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(_parse_event, events))
        return [r for r in results if r is not None]
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
        ("Michigan State Spartans", "Michigan State"),
        ("Michigan Wolverines", "Michigan"),
        ("Minnesota Golden Gophers", "Minnesota"),
        ("Mississippi State Bulldogs", "Mississippi State"),
        ("Missouri Tigers", "Missouri"),
        ("NC State Wolfpack", "NC State"),
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
    # Fallback: first word
    parts = name.split()
    if parts:
        return parts[0]
    return name


_normalize_team_name._name_map = []  # populated lazily on first call to avoid per-call rebuild


def _build_odds_map(odds_data: list[dict]) -> dict:
    """Build a lookup dict keyed by (home_team, away_team) -> odds summary.

    Records the sportsbook name the line came from. Book preference order
    (sharpest/most standard first): pinnacle, draftkings, fanduel, betrivers,
    bovada. Falls back to the first book that offers a spread.
    """
    _BOOK_PRIORITY = ["pinnacle", "draftkings", "fanduel", "betrivers", "bovada"]
    odds_map = {}
    for game in odds_data:
        home = _normalize_team_name(game.get("home_team", ""))
        away = _normalize_team_name(game.get("away_team", ""))
        key = (home, away)
        home_short = home.lower()
        away_short = away.lower()
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
        # Pick the best book by priority, else the first available
        chosen = None
        for bk in _BOOK_PRIORITY:
            if bk in per_book:
                chosen = bk
                break
        if chosen is None and per_book:
            chosen = next(iter(per_book))
        b = per_book.get(chosen, {})
        odds_map[key] = {
            "spread": round(b.get("spread"), 1) if b.get("spread") is not None else None,
            "total": round(b.get("total"), 1) if b.get("total") is not None else None,
            "spread_home_favorite": b.get("spread") is not None and b.get("spread") < 0,
            "book": chosen,
            "book_title": b.get("title", chosen),
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
    odds_map = {}
    # 1) PropLine (primary)
    try:
        pl = _propline_fetch()
        pl_map = _build_odds_map(pl)
        for k, v in pl_map.items():
            v["source"] = "propline"
        odds_map.update(pl_map)
    except Exception as e:
        print(f"[Odds] PropLine primary failed: {e}")
    # 2) The Odds API (backup — only fill gaps)
    try:
        toa = _the_odds_fetch()
        for k, v in _build_odds_map(toa).items():
            if k not in odds_map:
                v["source"] = "the_odds_api"
                odds_map[k] = v
    except Exception as e:
        print(f"[Odds] The Odds API backup failed: {e}")
    # 3) CFBD lines (last resort)
    try:
        for game in _cfbd_lines():
            hk = _normalize_team_name(game.get("homeTeam", ""))
            ak = _normalize_team_name(game.get("awayTeam", ""))
            key = (hk, ak)
            if key not in odds_map and game.get("lines"):
                line = game["lines"][0]
                odds_map[key] = {
                    "spread": round(line.get("spread"), 1) if line.get("spread") is not None else None,
                    "total": round(line.get("overUnder"), 1) if line.get("overUnder") is not None else None,
                    "spread_home_favorite": line.get("spread") is not None and line.get("spread") < 0,
                    "source": "cfbd",
                }
    except Exception as e:
        print(f"[Odds] CFBD lines fallback failed: {e}")
    _cache_set(_odds_cache, odds_map)
    _odds_disk_cache_set(odds_map)
    return odds_map


def project_score_multi_factor(team_data: dict, is_home: bool = True) -> dict:
    """
    Multi-factor projected score model.
    Uses actual cached fields: sp_plus (CFBD -40..+40 scale), fpi_win_prob, cpi,
    recruiting_rank, real elo.
    Weights: SP+ 25%, FPI Win Prob 20%, CPI 15%, Elo 8%, Recruiting 12%, EPA/Returning 20%
    Plus home-field advantage adjustment.
    Returns projected score, win probability, and composite rating.
    """
    # Extract ratings with defaults from actual cached data
    # IMPORTANT: use `or default` semantics — stored 0/None means "no data"
    # and must fall back to a neutral value, not be treated as a real rating.
    sp_plus = team_data.get("sp_plus") or 0.0       # CFBD real scale ~ -40..+40, 0 = average
    fpi_wp = team_data.get("fpi_win_prob") or 50.0  # 0-100 scale
    cpi = team_data.get("cpi") or 50.0              # 0-100 composite index
    rec_rank = team_data.get("recruiting_rank") or 80
    elo = team_data.get("elo") or 1500              # real Elo, ~1500 = average
    # Real season stats may be None when CFBD has no data (e.g. pre-season);
    # fall back to neutral league-average values only in that case.
    off_ppg = team_data.get("off_ppg") or 28.0    # points scored per game
    def_ppg = team_data.get("def_ppg") or 24.0    # points allowed per game

    # Normalize each factor to a 0-100 scale
    # SP+: CFBD range roughly -40 (bad) to +40 (elite), center 0 -> 0-100
    sp_norm = max(0, min(100, (sp_plus + 40) / 80 * 100))
    # FPI Win Prob: already 0-100
    fpi_norm = max(0, min(100, fpi_wp))
    # CPI: already 0-100
    cpi_norm = max(0, min(100, cpi))
    # Elo: real rating, ~1200-2000 range, center 1500 -> 0-100 (1500 = 50)
    elo_norm = max(0, min(100, (elo - 1500) / 300 * 100 + 50))
    # Recruiting: rank 1-130 -> 0-100 (inverted)
    rec_norm = max(0, min(100, (130 - rec_rank) / 129 * 100))

    # EPA / Returning / Experience bucket (20% combined)
    # Offensive EPA/play: positive values are good; scale ~ -1..+1 -> 0-100
    epa = team_data.get("epa_play") or 0.0
    epa_off_norm = max(0, min(100, (epa + 1) / 2 * 100))
    # Defensive EPA/play: LOWER is better; invert
    def_epa = team_data.get("def_epa_play") or 0.0
    epa_def_norm = max(0, min(100, (-def_epa + 1) / 2 * 100))
    # Returning production (% EPA returning): already 0-100
    pct_ret = team_data.get("pct_ppa_returning")
    ret_norm = max(0, min(100, (pct_ret or 0)))
    # Experience score: already 0-100
    exp = team_data.get("experience_score") or 0
    exp_norm = max(0, min(100, exp))

    # Average of the 4 sub-factors within the EPA bucket
    epa_bucket = (epa_off_norm + epa_def_norm + ret_norm + exp_norm) / 4

    # Weighted composite (0-100)
    # Original: SP+ 30%, FPI 25%, CPI 20%, Elo 10%, Recruiting 15% (100%)
    # New: SP+ 25%, FPI 20%, CPI 15%, Elo 8%, Recruiting 12%, EPA/Returning 20% (100%)
    composite = (
        sp_norm * 0.25 +
        fpi_norm * 0.20 +
        cpi_norm * 0.15 +
        elo_norm * 0.08 +
        rec_norm * 0.12 +
        epa_bucket * 0.20
    )

    # Projected score: calibrated to realistic CFB scoring.
    # A team's projected points vs an average opponent ranges ~12 (worst) to ~40 (elite).
    # League average is ~28 PPG. Composite 50 (average) -> ~27 pts.
    # The composite already encodes overall strength, so we do NOT add a second
    # offensive/defensive differential term on top (that was double-counting).
    # Anchor: 14 + (composite/100)*26 -> composite 50 = 27, 90 = 37.4, 20 = 19.2
    base_score = 14.0 + (composite / 100.0) * 26.0
    # Mild offensive/defensive tilt on top (small, to avoid re-double-counting):
    # a strong offense / weak defense nudges the projection up a few points.
    net_ppg = (off_ppg or 28.0) - (def_ppg or 24.0)
    base_score += max(-4.0, min(4.0, net_ppg * 0.15))
    # Home field advantage: ~2.5 points (CFBD research average)
    home_adj = 2.5 if is_home else -1.5
    projected_score = round(base_score + home_adj, 1)

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
        "sp_contribution": round(sp_norm * 0.25, 1),
        "fpi_contribution": round(fpi_norm * 0.20, 1),
        "cpi_contribution": round(cpi_norm * 0.15, 1),
        "elo_contribution": round(elo_norm * 0.08, 1),
        "rec_contribution": round(rec_norm * 0.12, 1),
        "epa_contribution": round(epa_bucket * 0.20, 1),
    }



def fetch_live_analytics():
    """Fetch and merge FPI, SP+, Recruiting, SRS, Elo, and REAL season stats from CFBD API."""
    try:
        teams_db = _cfbd_teams()
        fpi_data = _cfbd_fpi()
        sp_data = _cfbd_sp()
        rec_data = _cfbd_recruiting()
        srs_data = _cfbd_srs()
        elo_data = _cfbd_elo()
        real_stats = _cfbd_season_stats()

        # EPA (Expected Points Added) + possession-based metrics from CFBD
        ppa_data = _cfbd_ppa()
        drive_stats = _cfbd_drives_for_teams(list(
            teams_db.keys() | fpi_data.keys() | sp_data.keys() | rec_data.keys()))
        returning_data = _cfbd_returning()
        roster_exp = _cfbd_roster_experience()
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

            # Recruiting
            rec_rank = rec.get("rank", 0)
            rec_points = rec.get("points", 0)

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

            # EPA (Expected Points Added) + possession-based metrics from CFBD
            ppa = ppa_data.get(name, {})
            ds = drive_stats.get(name, {})
            epa_overall = ppa.get("epa_play")
            epa_pass = ppa.get("epa_pass")
            epa_rush = ppa.get("epa_rush")
            def_epa_overall = ppa.get("def_epa_play")
            def_epa_pass = ppa.get("def_epa_pass")
            def_epa_rush = ppa.get("def_epa_rush")
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
                "wins": 0,
                "losses": 0,
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
                "elo": round(elo_rating, 1),
                "recruiting_rank": rec_rank,
                "recruiting_pts": round(rec_points, 2),
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

@app.post("/api/analytics/fetch")
def api_analytics_fetch():
    """Force-fetch live analytics data from CFBD API and persist to disk."""
    import traceback
    try:
        # Do NOT touch the rankings cache — analytics lives in its own store
        analytics = fetch_live_analytics()
        if analytics:
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

# ── ESPN Schedule Fetcher ──
ESPN_SCHEDULE_URL = "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard"
TEAMS_NAMES_FILE = BASE_DIR / "data" / "team_names.json"
FBS_TEAMS_FILE = BASE_DIR / "data" / "fbs_teams.json"

# 2024 realignment override: ESPN API still returns old conferences for many teams
CONF_OVERRIDE = {
    # Moved to Big Ten
    "Oregon": "Big Ten", "Washington": "Big Ten", "USC": "Big Ten", "UCLA": "Big Ten",
    # Moved to SEC
    "Texas": "SEC", "Oklahoma": "SEC",
    # Moved to ACC
    "Cal": "ACC",
    # Moved to Big 12
    "BYU": "Big 12", "Cincinnati": "Big 12", "Houston": "Big 12", "UCF": "Big 12",
}

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

def load_team_names() -> dict:
    try:
        with open(TEAMS_NAMES_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def fetch_espn_schedule(week: int = 1, year: int = 2026) -> list[dict] | None:
    """Fetch weekly matchups from ESPN API and map to our team names."""
    try:
        url = f"{ESPN_SCHEDULE_URL}?year={year}&week={week}"
        data = _http_get(url, retries=3, base_delay=1.0)
        if not data:
            return None
        name_map = load_team_names()
        matchups = []
        for event in data.get("events", []):
            comp = event.get("competitions", [{}])[0]
            home = away = None
            for c in comp.get("competitors", []):
                loc = c.get("team", {}).get("location", "")
                if c.get("homeAway") == "home":
                    home = name_map.get(loc, loc)
                else:
                    away = name_map.get(loc, loc)
            if home and away:
                matchups.append({
                    "home": home,
                    "away": away,
                    "date": event.get("date"),  # ISO 8601, e.g. 2026-08-29T16:00Z
                })
        # Cache the raw ESPN data to disk for future fallback if the API goes down
        try:
            cache_file = BASE_DIR / "data" / f"espn_week{week}_{year}.json"
            cache_file.write_text(json.dumps(data), encoding="utf-8")
        except Exception:
            pass
        return matchups
    except Exception as e:
        print(f"[ESPN schedule fetch failed] {e}")
        return None

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
    # Fetch live odds once (PropLine primary, The Odds API backup, CFBD fallback)
    odds_map = _fetch_odds_map()
    enriched = []
    for m in sched.get("matchups", []):
        home = team_map.get(m["home"], {})
        away = team_map.get(m["away"], {})
        # Multi-factor projection
        home_proj_data = project_score_multi_factor(home, is_home=True)
        away_proj_data = project_score_multi_factor(away, is_home=False)
        diff = round(home_proj_data["projected_score"] - away_proj_data["projected_score"], 1)
        # Look up live betting line (negative spread = home is favorite)
        odds_key = (_normalize_team_name(m["home"]), _normalize_team_name(m["away"]))
        line = odds_map.get(odds_key, {})
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


@app.post("/api/schedule/update")
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

@app.post("/api/schedule/fetch")
def api_schedule_fetch(week: int = 1, year: int = 2026):
    """Fetch live schedule from ESPN and project scores from SP+."""
    matchups = fetch_espn_schedule(week, year)
    # Fallback: try cached ESPN data files if live fetch fails
    if not matchups:
        import glob
        cached_files = sorted(glob.glob(str(BASE_DIR / "data" / "espn_week*.json")))
        for cf in reversed(cached_files):
            try:
                with open(cf) as f:
                    cached = json.load(f)
                name_map = load_team_names()
                for event in cached.get("events", []):
                    comp = event.get("competitions", [{}])[0]
                    home = away = None
                    for c in comp.get("competitors", []):
                        loc = c.get("team", {}).get("location", "")
                        if c.get("homeAway") == "home":
                            home = name_map.get(loc, loc)
                        else:
                            away = name_map.get(loc, loc)
                    if home and away:
                        matchups.append({
                            "home": home,
                            "away": away,
                            "description": event.get("shortName", ""),
                            "date": event.get("date"),  # ISO 8601, e.g. 2026-08-29T16:00Z
                        })
                if matchups:
                    break
            except Exception:
                continue
    if not matchups:
        raise HTTPException(502, "ESPN schedule fetch failed")
    team_map = _build_team_map()
    conf_map = load_fbs_conferences()

    # Fetch live betting odds (PropLine primary, The Odds API + CFBD backup)
    odds_map = _fetch_odds_map()

    enriched = []
    for m in matchups:
        home = team_map.get(m["home"], {})
        away = team_map.get(m["away"], {})
        # Multi-factor projection
        home_proj_data = project_score_multi_factor(home, is_home=True)
        away_proj_data = project_score_multi_factor(away, is_home=False)
        home_proj = home_proj_data["projected_score"]
        away_proj = away_proj_data["projected_score"]
        diff = round(home_proj - away_proj, 1)
        # Betting market overlay
        odds_key = (_normalize_team_name(m["home"]), _normalize_team_name(m["away"]))
        market_odds = odds_map.get(odds_key, {})
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
            # Over/under projection
            "model_total": model_total,
            "total_vs_model": total_vs_model,
            "over_pick": over_pick,
            "home_record": f"{home.get('wins',0)}-{home.get('losses',0)}",
            "away_record": f"{away.get('wins',0)}-{away.get('losses',0)}",
            "home_conf": home_conf,
            "away_conf": away_conf,
            "home_logo_url": _LOGO_MAP.get(m["home"].lower()),
            "away_logo_url": _LOGO_MAP.get(m["away"].lower()),
        })
    # Lock in this week's SU + ATS picks (idempotent)
    _lock_picks(enriched, week)
    return {"week": week, "season": year, "updated": datetime.now().isoformat(), "matchups": enriched, "has_odds": len(odds_map) > 0}

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


def _compute_pick(home: str, away: str, diff: float, spread: float | None) -> dict:
    """Determine the projection's SU and ATS picks for a matchup.

    diff   = home_proj - away_proj (positive = home favored)
    spread = signed betting line (negative = home favorite), e.g. -7.5
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
    return {"su_pick": su_pick, "ats_pick": ats_pick}


def _lock_picks(enriched: list[dict], week: int) -> None:
    """Persist picks for matchups not yet locked (idempotent by matchup key)."""
    record = _load_record()
    existing = {p["key"] for p in record.get("picks", [])}
    changed = False
    for m in enriched:
        key = _record_key(m["home"], m["away"])
        if key in existing:
            continue
        spread = m.get("line_diff")
        diff = m.get("differential", 0)
        pk = _compute_pick(m["home"], m["away"], diff, spread)
        record["picks"].append({
            "key": key,
            "week": week,
            "home": m["home"],
            "away": m["away"],
            "date": m.get("date"),
            "spread": spread,
            "home_proj": m.get("home_proj"),
            "away_proj": m.get("away_proj"),
            **pk,
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


def _ingest_results() -> int:
    """Grade any locked picks that now have final scores. Returns # newly graded."""
    record = _load_record()
    finals = _fetch_final_scores()
    if not finals:
        return 0
    graded = {r.get("key") for r in record.get("results", [])}
    newly = 0
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
        su = _grade_su(p.get("su_pick"), p.get("home"), p.get("away"), home_score, away_score)
        ats = None
        if p.get("spread") is not None and p.get("ats_pick") is not None:
            ats = _grade_ats(p.get("ats_pick"), p.get("home"), p.get("away"),
                             p["spread"], home_score, away_score)
        record["results"].append({
            "key": p.get("key"),
            "week": p.get("week"),
            "home": p.get("home"), "away": p.get("away"),
            "home_score": home_score, "away_score": away_score,
            "su_pick": p.get("su_pick"), "ats_pick": p.get("ats_pick"),
            "spread": p.get("spread"),
            "su_result": su, "ats_result": ats,
        })
        newly += 1
    if newly:
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
    for r in record.get("results", []):
        for bucket, field in ((su, "su_result"), (ats, "ats_result")):
            v = r.get(field)
            if v == "W":
                bucket["wins"] += 1; bucket["graded"] += 1
            elif v == "L":
                bucket["losses"] += 1; bucket["graded"] += 1
            elif v == "push":
                bucket["pushes"] += 1
    return {
        "season": record.get("season"),
        "updated": record.get("updated"),
        "picks": record.get("picks", []),
        "results": record.get("results", []),
        "su": su,
        "ats": ats,
        "su_str": f"{su['wins']}-{su['losses']}" + (f"-{su['pushes']}" if su['pushes'] else ""),
        "ats_str": f"{ats['wins']}-{ats['losses']}" + (f"-{ats['pushes']}" if ats['pushes'] else ""),
    }


@app.post("/api/record/ingest")
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


@app.post("/api/refresh")
def api_refresh():
    """Manually trigger a full refresh (lines + final scores + grading)."""
    try:
        return refresh_all()
    except Exception as e:
        print(f"[refresh error] {e}")
        return {"error": str(e)}


@app.get("/api/line-movements")
def api_line_movements():
    """Get line movement history since the last snapshot."""
    try:
        movements = _snapshot_lines(_fetch_odds_map())
        return {"movements": movements, "count": len(movements)}
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
