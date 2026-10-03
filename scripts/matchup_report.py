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
        "User-Agent": "cto-matchup-report/1.0",
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


def render_html(out: dict, proj: dict | None = None, logos: dict | None = None) -> str:
    home, away = out["home"], out["away"]
    logos = logos or {}
    proj = proj or {}

    def row(r):
        a, h = r["away"], r["home"]

        def side_cls(side, stat):
            lead = "lead" if r["leader"] == side else ""
            return f"val {side} {_heat(stat.get('pct'))} {lead}".strip()

        def badge(stat):
            if stat["rank"] is None:
                return ""
            return f'<span class="rk {_pill(stat.get("pct"))}">{stat["rank"]}</span>'

        return (f'<tr><td class="{side_cls("away", a)}">'
                f'{_fmt(a["value"], r["decimals"])}{badge(a)}</td>'
                f'<td class="label">{r["label"]}</td>'
                f'<td class="{side_cls("home", h)}">{badge(h)}'
                f'{_fmt(h["value"], r["decimals"])}</td></tr>')

    sections = ""
    for sec in out["sections"]:
        w = sec["wins"]
        sections += (f'<section><h2>{sec["label"]}'
                     f'<span class="tally">away {w["away"]} · home {w["home"]}</span></h2>'
                     f'<table>{"".join(row(r) for r in sec["rows"])}</table></section>')

    edges = ""
    for e in out["edges"]:
        if e["label"] in _RETIRED_EDGE_LABELS:
            continue
        side = "away" if e["direction"] == "away_o_vs_home_d" else "home"
        off_team = away["name"] if side == "away" else home["name"]
        def_team = home["name"] if side == "away" else away["name"]
        lead = {"away": away["name"], "home": home["name"], "even": "even",
                None: "no data"}[e["leader"]]
        lead_cls = "" if e["leader"] is None else ' class="none"'
        off_stat, def_stat = _edge_sides(e)
        edges += (f'<li><b>{off_team} O</b> vs <b>{def_team} D</b> — {e["label"]}: '
                  f'{_fmt(off_stat["value"], e["decimals"])} vs '
                  f'{_fmt(def_stat["value"], e["decimals"])} → <em{lead_cls}>{lead}</em></li>')

    away_wp, home_wp = proj.get("away_win_prob"), proj.get("home_win_prob")
    away_pp, home_pp = proj.get("away_proj"), proj.get("home_proj")
    num = lambda v: isinstance(v, (int, float))  # noqa: E731

    def lead_cls(a, b):
        return "lead" if num(a) and num(b) and a > b else ""

    subs = {
        "{{TITLE}}": f"{away['name']} @ {home['name']} — Advanced Stats Preview",
        "{{AWA}}": away["name"], "{{HOM}}": home["name"],
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
        "{{SECTIONS}}": sections, "{{EDGES}}": edges,
    }
    tpl = TEMPLATE.read_text(encoding="utf-8")
    for k, v in subs.items():
        tpl = tpl.replace(k, v)
    return tpl


def render_png(html_text: str, png_path: str | Path, width: int = 800) -> str:
    """Screenshot the card with headless Edge, sized to its real content height."""
    exe = next((p for p in _EDGE_CANDIDATES if p and Path(p).exists()), None)
    if not exe:
        raise SystemExit("Edge not found for --png; set CFB_EDGE to msedge.exe")
    scratch = Path(os.environ.get("TMPDIR", ".")) / "matchup_card"
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
    height = (int(m.group(1)) + 24) if m else 1500

    page = scratch / "_page.html"
    page.write_text(html_text, encoding="utf-8")
    subprocess.run(
        [exe, "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
         "--force-device-scale-factor=2", f"--window-size={width},{height}",
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
    ap.add_argument("--no-proj", action="store_true",
                    help="skip the /api/schedule lookup (win prob / line / logos)")
    args = ap.parse_args()

    out = (compute_local(args.away, args.home) if args.local
           else fetch_api(args.api, args.away, args.home))

    proj, logos = None, None
    if not args.no_proj:
        proj = fetch_schedule_game(args.api, args.away, args.home, args.week)
        if proj:
            logos = {"away": logo_data_uri(proj.get("away_logo_url")),
                     "home": logo_data_uri(proj.get("home_logo_url"))}

    if args.html or args.png:
        html_text = render_html(out, proj, logos)
        if args.html:
            Path(args.html).write_text(html_text, encoding="utf-8")
            print(f"html: {args.html}", file=sys.stderr)
        if args.png:
            render_png(html_text, args.png)
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
