#!/usr/bin/env python3
"""Advanced-stat matchup CARD for one game -- Research's redesigned template.

NOTE: this is Research's variant, kept deliberately separate from CTO's
`scripts/matchup_report.py` (CTO is working the same card redesign in
parallel) so neither overwrites the other's output file. Same data contract
(/api/matchup, /api/schedule), different template + renderer:
`scripts/templates/matchup_card_research.html`.

    python scripts/matchup_report_research.py --away Washington --home USC
    python scripts/matchup_report_research.py --away Washington --home USC --html card.html
    python scripts/matchup_report_research.py --away Washington --home USC --png card.png
    python scripts/matchup_report_research.py --away Washington --home USC --json

Jeff's direction (2026-09-27): these metrics are something he asks for
personally, NOT a public site feature. Jeff's palette rule (2026-10-02):
label/text contrast is non-negotiable -- never grey-on-dark for a label or a
value; colored rank pills + red/green heat cells + team logos + win prob.

Changes vs the original card (readability pass, 2026-10-03):
  - Percentile gauge bar under every value, not just the rank pill -- lets you
    scan "how good" at a glance instead of reading a number.
  - Legend moved to the TOP of the card (was only in the footer) so every
    section below is self-explanatory on first look.
  - Section headers get an icon + a louder "away N / home N" tally.
  - Subtitle line (source · field size · as-of timestamp) now actually
    renders in the HTML card -- it previously only existed in the text report.
  - Edge list rendered as a two-line comparison with a colored winner chip
    instead of a plain inline sentence.

No CFBD quota is spent by any mode: /api/matchup reads stored analytics only,
and the projection/logo lookup rides the public /api/schedule the site
itself serves.
"""
from __future__ import annotations

import argparse
import base64
import colorsys
import io
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_API = "https://skeezcfb-rankings.com"
TEMPLATE = Path(__file__).resolve().parent / "templates" / "matchup_card_research.html"

# Percentile bands for the rank pill / heat cell: top band = blue pill + green
# heat, bottom band = red. Kept in one place so the pill and the heat agree.
GOOD_PCT, BAD_PCT = 70, 30

# Edge rows the API still returns but that compare two DIFFERENT scales, so their
# "leader" is meaningless. App.py has been fixed at the source; this keeps the card
# honest while prod still answers with the old tuple.
_RETIRED_EDGE_LABELS = {"EXPLOSIVENESS vs HAVOC ALLOWED"}

# Section label (substring match, case-insensitive) -> icon. Order matters:
# first match wins. Falls back to a generic bar-chart icon.
_SECTION_ICONS = (
    ("trench", "🧱"),
    ("havoc", "🧱"),
    ("drive", "🏁"),
    ("finish", "🏁"),
    ("situational", "⏱️"),
    ("down", "⏱️"),
    ("field position", "🧭"),
    ("special teams", "🧭"),
)

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


def _edge_sides(e: dict):
    """(offense_stat, defense_stat) for an edge row.

    The payload stores the two sides under 'away'/'home' for comparison, NOT in
    label order: for a home-offense edge the 'away' slot holds the DEFENSE. Render
    in label order (offense vs defense) or every home-offense row reads backwards.
    """
    if e["direction"] == "away_o_vs_home_d":
        return e["away"], e["home"]
    return e["home"], e["away"]


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
        "User-Agent": "research-matchup-report/1.0",
    })


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


_FALLBACK_ACCENT = (90, 100, 120)   # neutral slate RGB, used when a logo has no usable color


def _dominant_color_rgb(data_uri: str) -> tuple[int, int, int]:
    """Average logo color, skipping near-white/near-black/grey/transparent pixels.

    Team logos are almost always a colored mark on a white or transparent field with
    black outline strokes -- averaging ALL pixels would just produce grey. Filtering
    those out and then boosting saturation/clamping lightness gives a color that reads
    clearly as an accent against the card's dark background.
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
            if mx > 232 and mn > 205:           # near-white background
                continue
            if mx < 38:                          # near-black outline
                continue
            if mx - mn < 14 and mx < 205:         # low-saturation grey
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
    """Boost saturation + clamp lightness so the sampled color reads as a clean accent
    on a near-black card (a muddy dark-brown or washed-out pastel both fail that job)."""
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
    """Public entry point: logo data URI -> accent hex, sampled live, never hardcoded."""
    return _accent_hex(_dominant_color_rgb(logo_uri))


def _wp_ring(pct, accent: str, size: int = 76, stroke: int = 7) -> str:
    """SVG circular win-probability gauge, colored with the team's own sampled accent."""
    if not isinstance(pct, (int, float)):
        pct = 50.0
    pct = max(0.0, min(100.0, float(pct)))
    r = (size - stroke) / 2
    c = size / 2
    circumference = 2 * 3.14159265 * r
    dash = circumference * pct / 100
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}" class="wp-ring">'
        f'<circle cx="{c}" cy="{c}" r="{r}" fill="none" stroke="rgba(255,255,255,.09)" '
        f'stroke-width="{stroke}"/>'
        f'<circle cx="{c}" cy="{c}" r="{r}" fill="none" stroke="{accent}" stroke-width="{stroke}" '
        f'stroke-linecap="round" stroke-dasharray="{dash:.1f} {circumference:.1f}" '
        f'transform="rotate(-90 {c} {c})"/>'
        f'<text x="{c}" y="{c + 5}" text-anchor="middle" font-size="17" font-weight="800" '
        f'fill="#fff">{pct:.0f}%</text></svg>'
    )


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
    """ASCII bar for the text report only."""
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


def _section_icon(label: str) -> str:
    low = (label or "").lower()
    for needle, icon in _SECTION_ICONS:
        if needle in low:
            return icon
    return "📊"


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


def _subtitle(out: dict) -> str:
    parts = [out.get("source") or "stored-analytics"]
    if out.get("field_size"):
        parts.append(f"{out['field_size']}-team field")
    if out.get("as_of"):
        parts.append(f"as of {out['as_of'][:19].replace('T', ' ')} UTC")
    return " · ".join(parts)


def _side_to_team(side, home: str, away: str) -> str:
    """projected_winner / ats_pick / over_pick come back as side indicators
    ('home'/'away'), not team names -- translate before ever putting them on the card."""
    low = (side or "").strip().lower()
    if low == "home":
        return home
    if low == "away":
        return away
    return side or ""


def _predict_banner(proj: dict | None, home: str, away: str) -> str:
    """Headline strip: who the model favors and by how much. Empty string (renders
    nothing) when the game isn't on a served slate -- no fabricated prediction."""
    if not proj:
        return ""
    diff = proj.get("differential")
    winner = _side_to_team(proj.get("projected_winner"), home, away)
    if isinstance(diff, (int, float)) and winner:
        return (f'<div class="predict"><span class="bolt">🔮</span>'
                f'Model favors <b>{winner}</b> by <b>{abs(diff):.1f}</b></div>')
    hwp, awp = proj.get("home_win_prob"), proj.get("away_win_prob")
    if isinstance(hwp, (int, float)) and isinstance(awp, (int, float)):
        fav, pct = (home, hwp) if hwp >= awp else (away, awp)
        return (f'<div class="predict"><span class="bolt">🔮</span>'
                f'Model favors <b>{fav}</b> ({pct:.0f}% win prob)</div>')
    return ""


def _pick_chips(proj: dict | None, home: str, away: str) -> str:
    """ATS / total angle chips -- Jeff's betting-model angle, only shown when real."""
    if not proj:
        return ""
    chips = []
    ats = proj.get("ats_pick")
    if ats:
        chips.append(f'<span class="pickchip"><span class="k">ATS</span>'
                      f'{_side_to_team(ats, home, away)}</span>')
    over = proj.get("over_pick")
    if over:
        chips.append(f'<span class="pickchip"><span class="k">TOTAL</span>{over.title()}</span>')
    if proj.get("is_star_pick"):
        chips.append('<span class="pickchip"><span class="k">⭐</span>Star pick</span>')
    return f'<div class="chiprow">{"".join(chips)}</div>' if chips else ""


def render_html(out: dict, proj: dict | None = None, logos: dict | None = None) -> str:
    home, away = out["home"], out["away"]
    logos = logos or {}
    proj = proj or {}

    away_logo = logos.get("away") or _BLANK_PX
    home_logo = logos.get("home") or _BLANK_PX
    away_accent = team_accent(away_logo)
    home_accent = team_accent(home_logo)

    def row(r):
        a, h = r["away"], r["home"]

        def side_cls(side, stat):
            lead = "lead" if r["leader"] == side else ""
            return f"val {side} {_heat(stat.get('pct'))} {lead}".strip()

        def badge(stat):
            if stat["rank"] is None:
                return ""
            return f'<span class="rk {_pill(stat.get("pct"))}">{stat["rank"]}</span>'

        def gauge(stat):
            pct = stat.get("pct")
            width = 0 if pct is None else max(2, min(100, round(pct)))
            return (f'<div class="pct-track"><div class="pct-fill {_heat(pct)}" '
                    f'style="width:{width}%"></div></div>')

        return (f'<tr><td class="{side_cls("away", a)}">'
                f'<div class="vwrap"><span class="vnum">{_fmt(a["value"], r["decimals"])}</span>'
                f'{badge(a)}</div>{gauge(a)}</td>'
                f'<td class="label">{r["label"]}</td>'
                f'<td class="{side_cls("home", h)}">'
                f'<div class="vwrap"><span class="vnum">{_fmt(h["value"], r["decimals"])}</span>'
                f'{badge(h)}</div>{gauge(h)}</td></tr>')

    sections = ""
    for sec in out["sections"]:
        w = sec["wins"]
        a_win = "away-win" if w["away"] > w["home"] else ""
        h_win = "home-win" if w["home"] > w["away"] else ""
        rows = sec["rows"]
        # Split into two side-by-side half-tables once a section is tall enough to
        # matter (4+ rows) -- this is what keeps the card from rendering as one very
        # tall narrow column (see the .card CSS comment: Jeff found the old 1-col
        # layout letterboxed on an iPhone). Short sections stay single-column so a
        # near-empty second column doesn't look broken.
        if len(rows) >= 4:
            mid = (len(rows) + 1) // 2
            table_html = (f'<div class="dual-table">'
                          f'<table>{"".join(row(r) for r in rows[:mid])}</table>'
                          f'<table>{"".join(row(r) for r in rows[mid:])}</table></div>')
        else:
            table_html = f'<table>{"".join(row(r) for r in rows)}</table>'
        sections += (
            f'<section class="block"><h2><span class="htitle"><span class="icon">'
            f'{_section_icon(sec["label"])}</span>{sec["label"]}</span>'
            f'<span class="tally"><span class="n {a_win}">{w["away"]}</span>'
            f'<span class="n {h_win}">{w["home"]}</span></span></h2>'
            f'{table_html}</section>')

    edges = ""
    for e in out["edges"]:
        if e["label"] in _RETIRED_EDGE_LABELS:
            continue
        side = "away" if e["direction"] == "away_o_vs_home_d" else "home"
        off_team = away["name"] if side == "away" else home["name"]
        def_team = home["name"] if side == "away" else away["name"]
        if e["leader"] == "away":
            chip_cls, lead = "chip away-win", away["name"]
        elif e["leader"] == "home":
            chip_cls, lead = "chip home-win", home["name"]
        elif e["leader"] == "even":
            chip_cls, lead = "chip none", "even"
        else:
            chip_cls, lead = "chip none", "no data"
        off_stat, def_stat = _edge_sides(e)
        edges += (
            f'<div class="edge-card"><div class="em"><b>{off_team}</b> O vs '
            f'<b>{def_team}</b> D — {e["label"]}</div>'
            f'<div class="ev"><span>{_fmt(off_stat["value"], e["decimals"])} vs '
            f'{_fmt(def_stat["value"], e["decimals"])}</span>'
            f'<span class="{chip_cls}">{lead}</span></div></div>'
        )

    away_wp, home_wp = proj.get("away_win_prob"), proj.get("home_win_prob")
    away_pp, home_pp = proj.get("away_proj"), proj.get("home_proj")
    num = lambda v: isinstance(v, (int, float))  # noqa: E731

    subs = {
        "{{TITLE}}": f"{away['name']} @ {home['name']} — Advanced Stats Preview",
        "{{AWA}}": away["name"], "{{HOM}}": home["name"],
        "{{AWAY_LOGO}}": away_logo, "{{HOME_LOGO}}": home_logo,
        "{{AWAY_REC}}": proj.get("away_record") or "",
        "{{HOME_REC}}": proj.get("home_record") or "",
        "{{AWAY_PP}}": f"{away_pp:.1f}" if num(away_pp) else "—",
        "{{HOME_PP}}": f"{home_pp:.1f}" if num(home_pp) else "—",
        "{{AWAY_WP_RING}}": _wp_ring(away_wp, away_accent),
        "{{HOME_WP_RING}}": _wp_ring(home_wp, home_accent),
        "{{LINES}}": _lines_block(proj, home["name"], away["name"]),
        "{{SUBTITLE}}": _subtitle(out),
        "{{PREDICT_BANNER}}": _predict_banner(proj, home["name"], away["name"]),
        "{{PICK_CHIPS}}": _pick_chips(proj, home["name"], away["name"]),
        "{{AWAY_ACCENT}}": away_accent, "{{HOME_ACCENT}}": home_accent,
        "{{AWAY_SOFT}}": _hex_to_rgba(away_accent, 0.14),
        "{{HOME_SOFT}}": _hex_to_rgba(home_accent, 0.14),
        "{{AWAY_GLOW}}": _hex_to_rgba(away_accent, 0.55),
        "{{HOME_GLOW}}": _hex_to_rgba(home_accent, 0.55),
        "{{SECTIONS}}": sections, "{{EDGES}}": edges,
    }
    tpl = TEMPLATE.read_text(encoding="utf-8")
    for k, v in subs.items():
        tpl = tpl.replace(k, v)
    return tpl


def render_png(html_text: str, png_path: str | Path, width: int = 1100, scale: int = 3) -> str:
    """Screenshot the card with headless Edge, sized to its real content height.

    `scale` is the device-pixel-ratio Edge renders at (--force-device-scale-factor).
    Default 3x: on an 840px-wide card that's a 2520px-wide PNG -- sharp when viewed
    full-size or zoomed on a phone. Bumped from 2x after Jeff found 2x hard to read
    on an iPhone (2026-10-03). Higher costs more render time/file size, not quality.
    """
    exe = next((p for p in _EDGE_CANDIDATES if p and Path(p).exists()), None)
    if not exe:
        raise SystemExit("Edge not found for --png; set CFB_EDGE to msedge.exe")
    scratch = Path(os.environ.get("TMPDIR", ".")) / "matchup_card_research"
    scratch.mkdir(parents=True, exist_ok=True)

    probe = scratch / "_probe.html"
    probe.write_text(
        html_text.replace("</body>",
                          '<script>document.title="H="+document.documentElement.scrollHeight;'
                          "</script></body>"),
        encoding="utf-8")
    dom = subprocess.run(
        [exe, "--headless=new", "--disable-gpu", "--no-sandbox",
         f"--user-data-dir={scratch / 'edge_probe'}", "--virtual-time-budget=4000",
         "--dump-dom", probe.as_uri()],
        capture_output=True, text=True, timeout=120)
    m = re.search(r"H=(\d+)", dom.stdout or "")
    height = (int(m.group(1)) + 24) if m else 1600

    page = scratch / "_page.html"
    page.write_text(html_text, encoding="utf-8")
    subprocess.run(
        [exe, "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
         f"--force-device-scale-factor={scale}", f"--window-size={width},{height}",
         f"--user-data-dir={scratch / 'edge_shot'}", f"--screenshot={png_path}",
         page.as_uri()],
        capture_output=True, text=True, timeout=120)
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
    ap.add_argument("--scale", type=int, default=3,
                    help="PNG device-pixel-ratio (default 3 = sharp on a retina phone screen)")
    ap.add_argument("--no-proj", action="store_true",
                    help="skip the /api/schedule lookup (win prob / line / logos)")
    args = ap.parse_args()

    # Fetch the schedule entry FIRST: /api/schedule matches this team pair
    # regardless of which order --home/--away were typed in, and returns the
    # REAL home/away labeling for the game. If the stat call (/api/matchup,
    # which has no such canonicalization -- it echoes back whatever order it's
    # given) is then made with a different order than this, the logos/records
    # end up labeled under the wrong team name even though each individual
    # payload is internally correct. Bug found 2026-10-03 (Ohio State @
    # Michigan rendered with the logos swapped) -- fix: always let the
    # schedule's real home/away win when both are available.
    proj, logos = None, None
    if not args.no_proj:
        proj = fetch_schedule_game(args.api, args.away, args.home, args.week)
        if proj:
            logos = {"away": logo_data_uri(proj.get("away_logo_url")),
                     "home": logo_data_uri(proj.get("home_logo_url"))}

    home_name, away_name = args.home, args.away
    if proj and proj.get("home") and proj.get("away"):
        home_name, away_name = proj["home"], proj["away"]
        if _norm(home_name) != _norm(args.home):
            print(f"[matchup] schedule says {home_name} is home / {away_name} is away "
                  f"(you passed --home {args.home} --away {args.away}) -- using the "
                  f"schedule's real order so logos/records/stats all agree.",
                  file=sys.stderr)

    out = (compute_local(away_name, home_name) if args.local
           else fetch_api(args.api, away_name, home_name))

    if args.html or args.png:
        html_text = render_html(out, proj, logos)
        if args.html:
            Path(args.html).write_text(html_text, encoding="utf-8")
            print(f"html: {args.html}", file=sys.stderr)
        if args.png:
            render_png(html_text, args.png, scale=args.scale)
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
