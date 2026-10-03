"""Everything a matchup card needs, read straight from D1. ZERO API calls.

WHY THIS EXISTS
---------------
A matchup card used to depend on live calls: CFBD for the advanced stats (via
`_cfbd_advanced_stats`) and the CFBD logo CDN for team crests. The stats half was worse
than slow -- when the CFBD call came back empty, `team_analytics_rows` SKIPS absent
values, so the 36 advanced keys were dropped from the published snapshot's KEY LIST
entirely and 22 of 34 matchup rows rendered blank while D1 held every value.

So this module reads D1 only:
  * team records   -> the PUBLISHED snapshot (`d1_write_path.load_team_analytics`)
  * game panel     -> `slate_cache` (kind='schedule'), the same payload /api/schedule serves
  * logos          -> a local disk cache, fetched at most once ever

and it REFUSES to return an incomplete board. A partial card is worse than an error:
it looks authoritative and is wrong.

`COMPLETENESS` is defined by the engine itself -- every key in MATCHUP_METRICS must be
present in the publication's key list.
"""
from __future__ import annotations

import base64
import gzip
import json
import pathlib
import sys
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import d1_store  # noqa: E402
import d1_write_path  # noqa: E402
from matchup_engine import MATCHUP_METRICS  # noqa: E402

CFBD_YEAR = 2026
LOGO_DIR = REPO / "data" / "logo_cache"

REQUIRED_KEYS = tuple(m[0] for m in MATCHUP_METRICS)


class Incomplete(Exception):
    """The published board is missing metrics the card needs. Never render through this."""


def publication(season: int = CFBD_YEAR) -> dict:
    pub = d1_write_path.analytics_publication(season)
    if not pub:
        raise Incomplete(f"no analytics publication in D1 for {season}")
    return pub


def check_complete(pub: dict) -> list[str]:
    """Keys MATCHUP_METRICS needs that the published snapshot does NOT declare."""
    have = set(pub.get("keys") or [])
    return [k for k in REQUIRED_KEYS if k not in have]


def board(season: int = CFBD_YEAR) -> tuple[list[dict], dict]:
    """(team records, publication). Raises Incomplete rather than serving a partial board."""
    pub = publication(season)
    missing = check_complete(pub)
    if missing:
        raise Incomplete(
            f"published snapshot for {season} wk{pub.get('week')} is missing "
            f"{len(missing)} of {len(REQUIRED_KEYS)} matchup metrics: {missing[:8]}"
            f"{' ...' if len(missing) > 8 else ''}"
        )
    rows = d1_write_path.load_team_analytics(season, pub=pub)
    if not rows:
        raise Incomplete(f"published snapshot for {season} yielded no rows")
    return [dict(r) for r in rows], pub


def _slate_payload(week: int, season: int = CFBD_YEAR) -> dict | None:
    r = d1_store.query(
        "SELECT payload_gz FROM slate_cache WHERE kind='schedule' AND season=? AND week=?",
        [season, week],
    )
    if not r:
        return None
    raw = r[0]["payload_gz"]
    if isinstance(raw, str):
        raw = base64.b64decode(raw)
    for decode in (gzip.decompress, lambda b: b):
        try:
            return json.loads(decode(raw))
        except Exception:  # noqa: BLE001
            continue
    return None


def game_panel(away: str, home: str, season: int = CFBD_YEAR,
               week: int | None = None) -> dict | None:
    """The projection/line/weather/logo block for one game, from D1 slate_cache.

    Same payload shape /api/schedule serves, so the renderer needs no API. Order of the
    two names does not matter -- the game is matched on the unordered pair.
    """
    from matchup_engine import _norm_team_name
    want = {_norm_team_name(away), _norm_team_name(home)}
    weeks = ([week] if week else []) + list(range(1, 17))
    for w in weeks:
        payload = _slate_payload(w, season)
        if not payload:
            continue
        for m in payload.get("matchups") or []:
            if {_norm_team_name(m.get("away")), _norm_team_name(m.get("home"))} == want:
                return m
    return None


def logo_data_uri(url: str | None) -> str:
    """Team logo as a data URI, served from a LOCAL cache.

    The card embeds logos directly, and pulling them from the CFBD CDN at render time was
    a live external call in the render path. Fetch a given logo at most once ever, then
    serve it off disk. A miss with no network degrades to an empty pixel, not a failure.
    """
    import matchup_report as _mr  # for _BLANK_PX
    if not url:
        return _mr._BLANK_PX
    LOGO_DIR.mkdir(parents=True, exist_ok=True)
    key = "".join(c for c in url.rsplit("/", 1)[-1] if c.isalnum() or c in "._-")
    path = LOGO_DIR / key
    if path.exists() and path.stat().st_size > 0:
        return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:  # one-time warm
            data = r.read()
        path.write_bytes(data)
        return "data:image/png;base64," + base64.b64encode(data).decode("ascii")
    except Exception:  # noqa: BLE001
        return _mr._BLANK_PX
