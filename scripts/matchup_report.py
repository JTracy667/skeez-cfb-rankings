#!/usr/bin/env python3
"""Advanced-stat matchup CARD for one game -- Jeff's personal analysis tool.

Jeff's direction (2026-09-27): these metrics are something he asks CTO for, NOT a
public site feature. So this script is the delivery vehicle:

    python scripts/matchup_report.py --away Washington --home USC
    python scripts/matchup_report.py --away Washington --home USC --html card.html
    python scripts/matchup_report.py --away Washington --home USC --png card.png
    python scripts/matchup_report.py --away Washington --home USC --json

It calls the admin-gated /api/matchup on prod (reading ADMIN_TOKEN from the repo
.env) so the numbers come from the LIVE stored analytics rather than a stale local
copy, and renders (a) a text report suitable for Telegram, (b) a dark-theme HTML
card from scripts/templates/matchup_card.html, and (c) a PNG of that card via
headless Edge. `--local` skips the API and computes from the local
data/cfbd_analytics.json instead (used before a deploy).

Card design rule (Jeff, 2026-10-02): the two reference cards he gave (Oregon/USC
Advanced Stats Preview, and the SportsSource sheet) are HIGH CONTRAST -- white or
near-black text, colored rank pills, red/green heat cells, team logos, and the
model's win probability. Never grey-on-dark for a label or a value.

No CFBD quota is spent by any mode: /api/matchup reads stored analytics only, and
the projection/logo lookup rides the public /api/schedule the site itself serves.
"""
from __future__ import annotations

import argparse
import base64
import colorsys
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_API = "https://skeezcfb-rankings.com"
TEMPLATE = Path(__file__).resolve().parent / "templates" / "matchup_card.html"

# Percentile bands for the rank pill / heat cell: top band = blue pill + green
# heat, bottom band = red. Kept in one place so the pill and the heat agree.
GOOD_PCT, BAD_PCT = 70, 30

# Edge rows the API still returns but that compare two DIFFERENT scales, so their
# "leader" is meaningless. App.py has been fixed at the source; this keeps the card
# honest while prod still answers with the old tuple.
_RETIRED_EDGE_LABELS = {"EXPLOSIVENESS vs HAVOC ALLOWED"}

_EDGE_CANDIDATES = (
    os.environ.get("CFB_EDGE"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

# Cloudflare (error 1010) blocks urllib's default UA on the public routes, so both
# the schedule lookup and the logo fetch must present a browser UA.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 1x1 transparent gif: keeps the layout intact when a logo cannot be fetched.
_BLANK_PX = ("data:image/gif;base64,"
             "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")

# Accent used when a logo is missing/unreadable, or when Pillow is not installed.
# Deliberately a neutral blue rather than a team colour: a WRONG team colour looks
# like a bug, an obviously generic one does not.
_FALLBACK_ACCENT = (64, 120, 200)


def _dominant_color_rgb(data_uri: str) -> tuple[int, int, int]:
    """Average logo colour, skipping near-white / near-black / grey / transparent.

    Logos are almost always a coloured mark on a white or transparent field with black
    outline strokes, so averaging every pixel just yields grey. Filter those out, then
    boost saturation and clamp lightness (see `_accent_hex`) so the result actually
    reads as an accent against a near-black card.

    Pillow is imported INSIDE the function and wrapped: it is an optional dependency,
    so a missing Pillow (or a bad logo) degrades to the neutral fallback instead of
    killing the card. Adapted from Research's matchup_report_research.py.
    """
    if not data_uri or not data_uri.startswith("data:image") or data_uri == _BLANK_PX:
        return _FALLBACK_ACCENT
    try:
        from PIL import Image  # noqa: PLC0415 -- optional dependency, only needed here
        _, b64data = data_uri.split(",", 1)
        raw = base64.b64decode(b64data)
        im = Image.open(io.BytesIO(raw)).convert("RGBA")
        im.thumbnail((48, 48))
        r_sum = g_sum = b_sum = n = 0
        for r, g, b, a in im.getdata():
            if a < 128:
                continue
            mx, mn = max(r, g, b), min(r, g, b)
            if mx > 232 and mn > 205:            # near-white background
                continue
            if mx < 38:                          # near-black outline
                continue
            if mx - mn < 14 and mx < 205:        # low-saturation grey
                continue
            r_sum += r
            g_sum += g
            b_sum += b
            n += 1
        if n == 0:
            return _FALLBACK_ACCENT
        return (r_sum // n, g_sum // n, b_sum // n)
    except Exception:  # noqa: BLE001 -- a bad/missing logo must never kill the card
        return _FALLBACK_ACCENT


def _accent_hex(rgb: tuple[int, int, int]) -> str:
    """Saturation-boosted, lightness-clamped accent so it reads on a near-black card."""
    r, g, b = rgb
    h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    l = min(max(l, 0.45), 0.64)
    s = min(max(s, 0.55), 1.0)
    r2, g2, b2 = colorsys.hls_to_rgb(h, l, s)
    return f"#{int(round(r2 * 255)):02x}{int(round(g2 * 255)):02x}{int(round(b2 * 255)):02x}"


def _hex_to_rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


def team_accent(logo_uri: str) -> str:
    """Logo data URI -> accent hex. Sampled live per matchup, never hardcoded."""
    return _accent_hex(_dominant_color_rgb(logo_uri))


def _edge_sides(e: dict):
    """(offense_stat, defense_stat) for an edge row.

    The payload stores the two sides under 'away'/'home' for comparison, NOT in
    label order: for a home-offense edge the 'away' slot holds the DEFENSE. Render
    in label order (offense vs defense) or every home-offense row reads backwards.
    """
    if e["direction"] == "away_o_vs_home_d":
        return e["away"], e["home"]
    return e["home"], e["away"]


from matchup_engine import matchup_breakdown  # single shared implementation


def admin_token() -> str:
    tok = os.environ.get("ADMIN_TOKEN", "").strip()
    if tok:
        return tok
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("ADMIN_TOKEN"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("ADMIN_TOKEN not found in environment or repo .env")


def _get_json(url: str, headers: dict | None = None, timeout: int = 60):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")[:300]
        raise SystemExit(f"{url} -> HTTP {e.code} {body}")


def fetch_api(api: str, away: str, home: str) -> dict:
    url = (f"{api.rstrip('/')}/api/matchup?home={urllib.parse.quote(home)}"
           f"&away={urllib.parse.quote(away)}")
    return _get_json(url, {
        "X-Admin-Token": admin_token(),
        "Accept": "application/json",
        "User-Agent": "cto-matchup-report/1.0",
    })


_ABBR_PATH = Path(__file__).resolve().parent.parent / "data" / "team_abbr.json"
_ABBR_CACHE: dict[str, str] = {}


def _abbr(name: str) -> str:
    """CFBD's own team abbreviation, for tight spots where the full name truncates.

    Canonical codes disambiguate where common shorthand does not: Mississippi State
    is MSST while Michigan State is MSU. Generated from CFBD /teams into
    data/team_abbr.json, so they are a known quantity rather than hand-written.
    Falls back to a derived code so a missing team can never blank the key.
    """
    global _ABBR_CACHE
    if not _ABBR_CACHE:
        try:
            _ABBR_CACHE = json.loads(_ABBR_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 -- optional file, must never be fatal
            _ABBR_CACHE = {}
    key = (name or "").strip()
    if key in _ABBR_CACHE:
        return _ABBR_CACHE[key]
    for k, v in _ABBR_CACHE.items():          # tolerate casing/punctuation drift
        if _norm(k) == _norm(key):
            return v
    return "".join(c for c in key.upper() if c.isalpha())[:4] or "—"


def _norm(name: str) -> str:
    return re.sub(r"[^a-z]", "", (name or "").lower())


def fetch_schedule_game(api: str, away: str, home: str, week: int | None = None):
    """The model's numbers for this game (win prob, proj pts, line), from /api/schedule.

    Public endpoint the site itself serves -- no admin token, and no CFBD call on
    the cached path. Returns None if the game is not on a served slate.
    """
    weeks = [week] if week else [None] + list(range(1, 17))
    for w in weeks:
        url = f"{api.rstrip('/')}/api/schedule" + (f"?week={w}" if w else "")
        try:
            data = _get_json(url, {"Accept": "application/json", "User-Agent": UA}, timeout=45)
        except SystemExit:
            continue
        for m in data.get("matchups") or []:
            if {_norm(m.get("home")), _norm(m.get("away"))} == {_norm(home), _norm(away)}:
                return m
    return None


def fetch_analytics_teams(api: str) -> dict:
    """name -> analytics record, from the PUBLIC /api/analytics.

    Used only for the ratings comparison (SP+/FPI/Elo/SRS + composite). The matchup
    endpoint deliberately returns just the two teams' comparison rows, so the ratings
    come from the same public payload the site's own pages read. Returns {} on any
    failure -- a ratings block is nice-to-have and must never kill a card.
    """
    try:
        data = _get_json(f"{api.rstrip('/')}/api/analytics",
                         {"Accept": "application/json", "User-Agent": UA}, timeout=90)
    except Exception:  # noqa: BLE001
        return {}
    return {t.get("name"): t for t in (data.get("teams") or []) if isinstance(t, dict)}


def logo_data_uri(url: str) -> str:
    """Inline the logo so the PNG renders even with no network at screenshot time."""
    if not url:
        return _BLANK_PX
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            ctype = r.headers.get("Content-Type", "image/png").split(";")[0]
        return f"data:{ctype};base64,{base64.b64encode(raw).decode()}"
    except Exception:  # noqa: BLE001 -- a missing logo must not kill the card
        return _BLANK_PX


def compute_local(away: str, home: str) -> dict:
    import app  # noqa: PLC0415 -- importing app starts a warm thread in prod only
    teams = app._matchup_teams()
    if not teams:
        raise SystemExit("no local analytics data (data/cfbd_analytics.json)")
    out = app.matchup_breakdown(home, away, teams)
    if out.get("error"):
        raise SystemExit(out["error"])
    out["source"] = "local-analytics"
    return out


def _fmt(val, dec: int) -> str:
    if val is None:
        return "—"
    if dec == 0:
        return f"{val:.0f}"
    return f"{val:.{dec}f}"


def _badge(stat: dict) -> str:
    if stat.get("rank") is None:
        return "—"
    return f"#{stat['rank']}"


def _bar(pct) -> str:
    if pct is None:
        return ""
    n = max(1, min(8, round(pct / 12.5)))
    return "█" * n


def _heat(pct) -> str:
    """Cell class from a percentile: top band green, bottom band red."""
    if pct is None:
        return ""
    return "good" if pct >= GOOD_PCT else ("bad" if pct <= BAD_PCT else "")


def _pill(pct) -> str:
    if pct is None:
        return "mid"
    return "" if pct >= GOOD_PCT else ("bad" if pct <= BAD_PCT else "mid")


def _cell(side: str, stat: dict, leader, dec: int) -> str:
    """One side of a comparison row: heat-tinted, leading side in green.

    Pills sit OUTSIDE the value (away: value then pill; home: pill then value) so
    the two numeric columns stay flush to their own edges, like the reference card.
    """
    lead = "lead" if leader == side else ""
    cls = f"val {side} {_heat(stat.get('pct'))} {lead}".strip()
    val = _fmt(stat.get("value"), dec)
    rk = ""
    if stat.get("rank") is not None:
        rk = f'<span class="rk {_pill(stat.get("pct"))}">{stat["rank"]}</span>'
    inner = f"{val}{rk}" if side == "away" else f"{rk}{val}"
    return f'<td class="{cls}">{inner}</td>'


def _row3(label: str, a: dict, h: dict, leader, dec: int) -> str:
    return (f'<tr>{_cell("away", a, leader, dec)}'
            f'<td class="label">{label}</td>'
            f'{_cell("home", h, leader, dec)}</tr>')


def render_text(out: dict) -> str:
    home, away = out["home"], out["away"]
    lines = [
        f"{away['name'].upper()} @ {home['name'].upper()} — ADVANCED MATCHUP",
        f"{out.get('source', '?')} · {out.get('field_size', '?')}-team field"
        + (f" · as of {out['as_of'][:19]}" if out.get("as_of") else ""),
        "",
        f"{'':<22}{'AWAY ' + away['name']:>26}{'HOME ' + home['name']:>26}",
    ]
    for sec in out["sections"]:
        w = sec["wins"]
        lines.append("")
        lines.append(f"── {sec['label']}  (away {w['away']} · home {w['home']})")
        for r in sec["rows"]:
            a, h = r["away"], r["home"]
            mark_a = "◀" if r["leader"] == "away" else " "
            mark_h = "▶" if r["leader"] == "home" else " "
            lines.append(
                f" {mark_a} {_fmt(a['value'], r['decimals']):>8} {_badge(a):>5} {_bar(a['pct']):<8}"
                f" {r['label']:<24}"
                f" {_bar(h['pct']):<8} {_badge(h):>5} {_fmt(h['value'], r['decimals']):>8} {mark_h}"
            )
    lines.append("")
    lines.append("── OFFENSE vs DEFENSE EDGES")
    for e in out["edges"]:
        if e["label"] in _RETIRED_EDGE_LABELS:
            continue
        side = "away" if e["direction"] == "away_o_vs_home_d" else "home"
        off_team = away["name"] if side == "away" else home["name"]
        def_team = home["name"] if side == "away" else away["name"]
        lead = {"away": away["name"], "home": home["name"], "even": "even", None: "n/a"}[e["leader"]]
        off_stat, def_stat = _edge_sides(e)
        lines.append(
            f"  {off_team} O vs {def_team} D — {e['label']}: {_fmt(off_stat['value'], e['decimals'])}"
            f" vs {_fmt(def_stat['value'], e['decimals'])}  → {lead}"
        )
    return "\n".join(lines)


def _lines_block(proj: dict | None, home: str, away: str) -> str:
    if not proj:
        return '<span class="k">LINE</span><br>not on slate'
    parts = []
    sp = proj.get("market_spread")
    if isinstance(sp, (int, float)):
        fav = home if sp < 0 else away
        parts.append(f'<span class="k">SPREAD</span> {fav} -{abs(sp):.1f}')
    tot = proj.get("market_total")
    mt = proj.get("model_total")
    if isinstance(tot, (int, float)):
        tail = f" (model {mt:.1f})" if isinstance(mt, (int, float)) else ""
        parts.append(f'<span class="k">TOTAL</span> {tot:.1f}{tail}')
    return "<br>".join(parts) or '<span class="k">LINE</span><br>n/a'


def _weather_block(proj: dict | None) -> str:
    """Kickoff conditions, rendered under the VS/line in the centre column.

    `indoor` takes precedence: a dome's temperature is meaningless, so show the fact
    that matters instead of a number nobody should read as a forecast.
    """
    wx = (proj or {}).get("weather") or {}
    if not wx:
        return "Weather n/a"
    if wx.get("indoor"):
        return "Indoor"
    parts = []
    temp = wx.get("temp")
    if isinstance(temp, (int, float)):
        parts.append(f"{temp:.0f}\u00b0F")
    cond = wx.get("condition")
    if cond:
        parts.append(str(cond))
    wind = wx.get("wind")
    if isinstance(wind, (int, float)):
        parts.append(f"wind {wind:.0f} mph")
    return " \u00b7 ".join(parts) or "Weather n/a"


def _legend_block() -> str:
    """The card's key.

    Every visual device on the card is defined here, in plain words: the heat tint,
    the green "holds the edge" value, the rank pill, the team-coloured edge rail, and
    the column convention.

    COPY MUST MATCH THE TEMPLATE'S ACTUAL LAYOUT. The previous wording ("outside
    columns = a team's own stats; centre tables = one side's OFFENSE vs the other's
    DEFENSE") described the retired three-column card and became a lie the moment the
    centre became a head-to-head ledger. Any layout change here needs a copy change.
    """
    return """
  <section><h2>Legend</h2>
    <div class="legend">
      <div class="lg"><span class="sw good"></span>Heat = that side's national percentile — green top 30%, red bottom 30%</div>
      <div class="lg"><span class="sw lead"></span>Green value = the side holding the edge on that row</div>
      <div class="lg"><span class="sw pill"></span>Pill = FBS national rank, 1 = best (blue top 30%, red bottom 30%, grey middle)</div>
      <div class="lg"><span class="sw rail"></span>Team-coloured rail beside a row = that team holds the edge (away left, home right)</div>
      <div class="lg"><span class="sw none"></span>Side panels = each team's own season profile; the centre ledger pairs every stat head-to-head — away value · label · home value</div>
      <div class="lg"><span class="sw none"></span>WIN PROB / PROJ PTS = the model · SPREAD / TOTAL = the market line (model total in brackets)</div>
    </div>
  </section>"""


def _ratings_block(rec_a: dict, rec_h: dict, away_name: str, home_name: str) -> str:
    """SP+ / FPI / Elo / SRS / composite for both teams.

    Plain stats comparison -- no model commentary. The card is an advanced-stats
    matchup of two teams, not an explanation of the projection; Jeff was explicit
    about that (2026-10-03).
    """
    rows = (
        ("SP+ RATING", "sp_plus", "sp_rank", 1),
        ("FPI", "fpi", "fpi_rank", 2),
        ("ELO", "elo", None, 0),
        ("SRS", "srs", None, 1),
        ("COMPOSITE", "composite", None, 1),
    )
    body = ""
    for label, key, rkey, dec in rows:
        va, vh = rec_a.get(key), rec_h.get(key)
        ra, rh = (rec_a.get(rkey), rec_h.get(rkey)) if rkey else (None, None)
        both = isinstance(va, (int, float)) and isinstance(vh, (int, float))
        # Higher is better for every rating here, so the leader is simply the larger.
        lead_a = "lead" if both and va > vh else ""
        lead_h = "lead" if both and vh > va else ""
        pa = f'<span class="rk mid">{ra}</span>' if ra else ""
        ph = f'<span class="rk mid">{rh}</span>' if rh else ""
        body += (f'<tr><td class="val away {lead_a}">{_fmt(va, dec)}{pa}</td>'
                 f'<td class="label">{label}</td>'
                 f'<td class="val home {lead_h}">{ph}{_fmt(vh, dec)}</td></tr>')
    return (f'<section><h2>{away_name} vs {home_name} — team ratings</h2>'
            f'<table>{body}</table></section>')


def render_html(out: dict, proj: dict | None = None, logos: dict | None = None,
                ratings: dict | None = None, template: str | Path | None = None) -> str:
    home, away = out["home"], out["away"]
    logos = logos or {}
    proj = proj or {}
    ratings = ratings or {}
    rec_a, rec_h = (ratings.get("away") or {}), (ratings.get("home") or {})
    ratings_html = (_ratings_block(rec_a, rec_h, away["name"], home["name"])
                    if rec_a and rec_h else "")
    # Team accents, sampled LIVE from each logo -- the card re-themes per matchup.
    away_accent = team_accent(logos.get("away") or _BLANK_PX)
    home_accent = team_accent(logos.get("home") or _BLANK_PX)

    def stack(side):
        """One team's own stats as a vertical column (the reference's side panels)."""
        html = ""
        for sec in out["sections"]:
            html += f'<div class="secttl">{sec["label"]}</div>'
            for r in sec["rows"]:
                st = r[side]
                lead = "lead" if r["leader"] == side else ""
                cls = f"sval {_heat(st.get('pct'))} {lead}".strip()
                val = _fmt(st.get("value"), r["decimals"])
                rk = ""
                if st.get("rank") is not None:
                    rk = f'<span class="rk {_pill(st.get("pct"))}">{st["rank"]}</span>'
                html += (f'<div class="srow {side}">'
                         f'<span class="slabel">{r["label"]}</span>'
                         f'<span class="{cls}">{val}</span>{rk}</div>')
        return html

    # The centre of the reference card: two head-to-head tables, each pairing one
    # side's OFFENSE against the other's DEFENSE. Left column = the AWAY team's
    # side of that pair, right = HOME's -- which is why the second table's left
    # column is a defense.
    by_dir: dict[str, list] = {}
    for e in out["edges"]:
        if e["label"] in _RETIRED_EDGE_LABELS:
            continue
        by_dir.setdefault(e["direction"], []).append(e)

    cross = ""
    for direction, title in ((("away_o_vs_home_d"), f"{away['name']} OFF vs {home['name']} DEF"),
                             (("home_o_vs_away_d"), f"{away['name']} DEF vs {home['name']} OFF")):
        rows = ""
        for e in by_dir.get(direction, []):
            off_stat, def_stat = _edge_sides(e)
            left, right = (off_stat, def_stat) if direction == "away_o_vs_home_d" else (def_stat, off_stat)
            rows += _row3(e["label"], left, right, e["leader"], e["decimals"])
        cross += f'<section><h2>{title}</h2><table>{rows}</table></section>'

    away_wp, home_wp = proj.get("away_win_prob"), proj.get("home_win_prob")
    away_pp, home_pp = proj.get("away_proj"), proj.get("home_proj")
    num = lambda v: isinstance(v, (int, float))  # noqa: E731

    def lead_cls(a, b):
        return "lead" if num(a) and num(b) and a > b else ""

    subs = {
        "{{TITLE}}": f"{away['name']} @ {home['name']} — Advanced Stats Preview",
        "{{AWA}}": away["name"], "{{HOM}}": home["name"],
        "{{AWAY_ABBR}}": _abbr(away["name"]), "{{HOME_ABBR}}": _abbr(home["name"]),
        "{{AWAY_LOGO}}": logos.get("away") or _BLANK_PX,
        "{{HOME_LOGO}}": logos.get("home") or _BLANK_PX,
        "{{AWAY_REC}}": proj.get("away_record") or "",
        "{{HOME_REC}}": proj.get("home_record") or "",
        "{{AWAY_WP}}": f"{away_wp:.1f}%" if num(away_wp) else "—",
        "{{HOME_WP}}": f"{home_wp:.1f}%" if num(home_wp) else "—",
        "{{AWAY_PP}}": f"{away_pp:.1f}" if num(away_pp) else "—",
        "{{HOME_PP}}": f"{home_pp:.1f}" if num(home_pp) else "—",
        "{{AWAY_WP_LEAD}}": lead_cls(away_wp, home_wp),
        "{{HOME_WP_LEAD}}": lead_cls(home_wp, away_wp),
        "{{AWAY_PP_LEAD}}": lead_cls(away_pp, home_pp),
        "{{HOME_PP_LEAD}}": lead_cls(home_pp, away_pp),
        "{{LINES}}": _lines_block(proj, home["name"], away["name"]),
        "{{WEATHER}}": _weather_block(proj),
        "{{CROSS}}": cross,
        "{{RATINGS}}": ratings_html,
        "{{LEGEND}}": _legend_block(),
        "{{AWAY_ACCENT}}": away_accent, "{{HOME_ACCENT}}": home_accent,
        "{{AWAY_SOFT}}": _hex_to_rgba(away_accent, 0.14),
        "{{HOME_SOFT}}": _hex_to_rgba(home_accent, 0.14),
        "{{AWAY_GLOW}}": _hex_to_rgba(away_accent, 0.55),
        "{{HOME_GLOW}}": _hex_to_rgba(home_accent, 0.55),
        "{{AWAY_STACK}}": stack("away"), "{{HOME_STACK}}": stack("home"),
    }
    tpl_path = Path(template) if template else TEMPLATE
    if not tpl_path.exists():
        raise SystemExit(f"template not found: {tpl_path}")
    tpl = tpl_path.read_text(encoding="utf-8")
    for k, v in subs.items():
        tpl = tpl.replace(k, v)
    return tpl


def render_png(html_text: str, png_path: str | Path, width: int = 1280,
               scale: int = 2) -> str:
    """Screenshot the card with headless Edge, sized to its real content height.

    `scale` is the device-pixel-ratio Edge renders at. Default 2 at this card WIDTH
    lands the PNG at ~2560px wide, which is Telegram's own photo ceiling -- anything
    larger is downscaled on delivery, so 3x here would just triple the file size for
    nothing. (Research ran 3x because his card was ~840px wide; same delivered
    resolution, different starting width. Jeff's "2x was hard to read" was against
    that narrow card, not this one.) Pass --scale 3 for a local-archive render.
    """
    exe = next((p for p in _EDGE_CANDIDATES if p and Path(p).exists()), None)
    if not exe:
        raise SystemExit("Edge not found for --png; set CFB_EDGE to msedge.exe")
    # Edge resolves --screenshot relative to ITS OWN cwd, so a relative path silently
    # writes nowhere (or somewhere else). Always hand it an absolute path.
    png_path = Path(png_path).resolve()
    scratch = Path(os.environ.get("TMPDIR", ".")) / "matchup_card"
    scratch.mkdir(parents=True, exist_ok=True)
    # A reused --user-data-dir can hold a stale lock from a killed run and hang the
    # probe forever; give every invocation its own profile dir.
    uniq = f"{os.getpid()}-{int(time.time())}"
    probe_dir = scratch / f"edge_probe_{uniq}"
    shot_dir = scratch / f"edge_shot_{uniq}"

    probe = scratch / f"_probe_{uniq}.html"
    probe.write_text(
        html_text.replace("</body>",
                          '<script>document.title="H="+document.documentElement.scrollHeight;'
                          "</script></body>"),
        encoding="utf-8")
    dom = subprocess.run(
        [exe, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-first-run",
         "--no-default-browser-check",
         # The probe MUST run at the same WIDTH as the screenshot. Without this it
         # measures at Edge's default ~800px viewport, so a fluid/responsive card
         # reports a height computed at the wrong width -- oversized (dead canvas)
         # or undersized (clipped). Cards that hard-lock body{width:1280px} happened
         # to be immune, which hid the bug. (Caught by CLO, 2026-10-03.)
         f"--window-size={width},600",
         f"--user-data-dir={probe_dir}", "--virtual-time-budget=4000",
         "--dump-dom", probe.as_uri()],
        capture_output=True, text=True, timeout=120)
    m = re.search(r"H=(\d+)", dom.stdout or "")
    height = (int(m.group(1)) + 24) if m else 1500

    page = scratch / f"_page_{uniq}.html"
    page.write_text(html_text, encoding="utf-8")
    shot_cmd = [exe, "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
                "--no-first-run", "--no-default-browser-check",
                "--force-device-scale-factor=%d" % scale, f"--window-size={width},{height}",
                f"--user-data-dir={shot_dir}", f"--screenshot={png_path}",
                page.as_uri()]
    # Back-to-back Edge launches occasionally exit 1 without writing the file -- observed
    # rendering four cards in a loop, where two of four silently produced nothing. Retry
    # once with a fresh profile dir rather than handing back a missing PNG.
    for attempt in (1, 2):
        subprocess.run(shot_cmd, capture_output=True, text=True, timeout=120)
        if Path(png_path).exists() and Path(png_path).stat().st_size > 0:
            break
        if attempt == 1:
            time.sleep(2)
            shutil.rmtree(shot_dir, ignore_errors=True)
    else:
        raise SystemExit(f"Edge produced no screenshot at {png_path}")
    for d in (probe_dir, shot_dir):
        shutil.rmtree(d, ignore_errors=True)
    return str(png_path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True)
    ap.add_argument("--away", required=True)
    ap.add_argument("--api", default=DEFAULT_API)
    ap.add_argument("--local", action="store_true")
    ap.add_argument("--week", type=int, help="schedule week holding the game")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--html", metavar="PATH")
    ap.add_argument("--png", metavar="PATH")
    ap.add_argument("--width", type=int, default=1280,
                    help="card width in CSS px (default 1240 = landscape for phone sharing)")
    ap.add_argument("--scale", type=int, default=2,
                    help="device-pixel-ratio for the PNG (default 2 = ~2560px, Telegram's photo ceiling)")
    ap.add_argument("--no-proj", action="store_true",
                    help="skip the /api/schedule lookup (win prob / line / logos)")
    ap.add_argument("--template", metavar="PATH",
                    help="render an ALTERNATE card template; default is "
                         "scripts/templates/matchup_card.html. Use this to try a redesign "
                         "without touching the canonical card.")
    ap.add_argument("--source", choices=("d1", "api"), default="d1",
                    help="where the data comes from. 'd1' (default) reads D1 directly and "
                         "makes NO network call to CFBD or the site. 'api' uses the site's "
                         "/api/matchup (debug/compare only).")
    args = ap.parse_args()

    proj, logos, ratings = None, None, None

    if args.source == "d1":
        # D1 ONLY: no CFBD call, no site call. Refuses to render a partial board rather
        # than emit a card that looks authoritative and is missing whole sections.
        from d1_board import Incomplete, board as d1_board, game_panel, logo_data_uri
        try:
            teams, pub = d1_board()
        except Incomplete as e:
            raise SystemExit(f"REFUSING to render a partial card: {e}")
        print(f"[d1] {len(teams)} teams | publication wk{pub.get('week')} | "
              f"{len(pub.get('keys') or [])} keys", file=sys.stderr)
        if not args.no_proj:
            proj = game_panel(args.away, args.home, week=args.week)
            if proj and _norm(proj.get("home")) != _norm(args.home):
                print(f"note: this game is {proj.get('away')} @ {proj.get('home')} -- "
                      f"labelling the sides from the schedule, not from argument order.",
                      file=sys.stderr)
                args.away, args.home = proj.get("away"), proj.get("home")
            if proj:
                logos = {"away": logo_data_uri(proj.get("away_logo_url")),
                         "home": logo_data_uri(proj.get("home_logo_url"))}
        out = matchup_breakdown(args.home, args.away, teams)
        if out.get("error"):
            raise SystemExit(out["error"])
        by_name = {t.get("name"): t for t in teams}
        ratings = {"away": by_name.get(out["away"]["name"]),
                   "home": by_name.get(out["home"]["name"])}
    else:
        # Orientation guard. This used to trust argument order, so building a card from
        # "A vs B" when the game is really "B @ A" silently put the logos, records,
        # projections and spread under the wrong team names -- a card that looks perfectly
        # fine and is wrong. /api/schedule knows the true venue, so resolve against it
        # before anything else is fetched.
        if not args.no_proj:
            proj = fetch_schedule_game(args.api, args.away, args.home, args.week)
            if proj and _norm(proj.get("home")) != _norm(args.home):
                print(f"note: this game is {proj.get('away')} @ {proj.get('home')} -- "
                      f"swapping the order you passed so both sides are labelled right.",
                      file=sys.stderr)
                args.away, args.home = proj.get("away"), proj.get("home")

        out = (compute_local(args.away, args.home) if args.local
               else fetch_api(args.api, args.away, args.home))

        if not args.no_proj:
            if proj:
                logos = {"away": logo_data_uri(proj.get("away_logo_url")),
                         "home": logo_data_uri(proj.get("home_logo_url"))}
            teams = fetch_analytics_teams(args.api)
            if teams:
                ratings = {"away": teams.get(out["away"]["name"]),
                           "home": teams.get(out["home"]["name"])}

    if args.html or args.png:
        html_text = render_html(out, proj, logos, ratings, template=args.template)
        if args.html:
            Path(args.html).write_text(html_text, encoding="utf-8")
            print(f"html: {args.html}", file=sys.stderr)
        if args.png:
            render_png(html_text, args.png, width=args.width, scale=args.scale)
            print(f"png: {args.png}", file=sys.stderr)

    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print(render_text(out))
        if not proj and not args.no_proj:
            print("\n[no schedule entry found — card omits win prob/line]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
