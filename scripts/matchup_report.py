#!/usr/bin/env python3
"""Advanced-stat matchup breakdown for one game -- Jeff's personal analysis tool.

Jeff's direction (2026-09-27): these metrics are something he asks CTO for, NOT a
public site feature. So this script is the delivery vehicle:

    python scripts/matchup_report.py --away Oregon --home USC
    python scripts/matchup_report.py --away Oregon --home USC --html out.html
    python scripts/matchup_report.py --away Oregon --home USC --json

It calls the admin-gated /api/matchup on prod (reading ADMIN_TOKEN from the repo
.env) so the numbers come from the LIVE stored analytics rather than a stale local
copy, and renders (a) a text report suitable for Telegram and (b) an optional
dark-theme HTML card. `--local` skips the API and computes from the local
data/cfbd_analytics.json instead (used before a deploy).

No CFBD quota is spent by any mode: the endpoint reads stored analytics only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_API = "https://skeezcfb-rankings.com"

# Higher = better gets a brighter badge; the point is that direction is decided by
# the engine's polarity flag, not by the number's size.
GOOD_PCT, BAD_PCT = 75, 25

# Edge rows the API still returns but that compare two DIFFERENT scales, so their
# "leader" is meaningless. App.py has been fixed at the source; this keeps the card
# honest while prod still answers with the old tuple.
_RETIRED_EDGE_LABELS = {"EXPLOSIVENESS vs HAVOC ALLOWED"}


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


def fetch_api(api: str, away: str, home: str) -> dict:
    url = f"{api.rstrip('/')}/api/matchup?home={urllib.parse.quote(home)}&away={urllib.parse.quote(away)}"
    req = urllib.request.Request(url, headers={
        "X-Admin-Token": admin_token(),
        "Accept": "application/json",
        "User-Agent": "cto-matchup-report/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")[:300]
        raise SystemExit(f"/api/matchup -> HTTP {e.code} {body}")


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


def render_html(out: dict) -> str:
    home, away = out["home"], out["away"]

    def row(r):
        a, h = r["away"], r["home"]
        av = _fmt(a["value"], r["decimals"])
        hv = _fmt(h["value"], r["decimals"])

        def cls(side):
            if r["leader"] is None:
                return ""
            return "lead" if r["leader"] == side else ""

        def badge(side, stat):
            if stat["rank"] is None:
                return ""
            return f'<span class="rk {cls(side)}">{stat["rank"]}</span>'

        return f"""<tr>
      <td class="val {cls('away')}">{av}{badge('away', a)}</td>
      <td class="label">{r['label']}</td>
      <td class="val {cls('home')}">{badge('home', h)}{hv}</td>
    </tr>"""

    sections = ""
    for sec in out["sections"]:
        w = sec["wins"]
        sections += f"""
  <section>
    <h2>{sec['label']}<span class="tally">away {w['away']} · home {w['home']}</span></h2>
    <table>{''.join(row(r) for r in sec['rows'])}</table>
  </section>"""

    edges = ""
    for e in out["edges"]:
        if e["label"] in _RETIRED_EDGE_LABELS:
            continue
        side = "away" if e["direction"] == "away_o_vs_home_d" else "home"
        off_team = away["name"] if side == "away" else home["name"]
        def_team = home["name"] if side == "away" else away["name"]
        lead = {"away": away["name"], "home": home["name"], "even": "even",
                None: "no data"}[e["leader"]]
        off_stat, def_stat = _edge_sides(e)
        edges += (f"<li><b>{off_team} O</b> vs <b>{def_team} D</b> — {e['label']}: "
                  f"{_fmt(off_stat['value'], e['decimals'])} vs "
                  f"{_fmt(def_stat['value'], e['decimals'])} → <em>{lead}</em></li>")

    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{away['name']} @ {home['name']} — Advanced Matchup</title>
<style>
:root{{--bg:#0b0d13;--surface:#141824;--border:#232838;--text:#e6e9f0;--muted:#8b93a7;
--accent:#4f8cff;--good:#22c55e;--bad:#ef4444}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--text);
font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;padding:18px}}
.card{{max-width:720px;margin:0 auto;background:var(--surface);border:1px solid var(--border);
border-radius:16px;padding:18px}}
.hd{{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap;
border-bottom:1px solid var(--border);padding-bottom:10px;margin-bottom:6px}}
h1{{font-size:19px;margin:0;font-weight:800}}
.meta{{color:var(--muted);font-size:11.5px}}
h2{{font-size:11.5px;letter-spacing:.08em;color:var(--accent);text-transform:uppercase;
margin:18px 0 6px;display:flex;justify-content:space-between;font-weight:700}}
.tally{{color:var(--muted);letter-spacing:0;text-transform:none;font-weight:500}}
table{{width:100%;border-collapse:collapse}}
td{{padding:5px 4px;border-bottom:1px solid rgba(255,255,255,.04);vertical-align:middle}}
.label{{text-align:center;color:var(--muted);font-size:10.5px;letter-spacing:.04em;white-space:nowrap}}
.val{{font-variant-numeric:tabular-nums;white-space:nowrap;width:44%}}
.val.home{{text-align:right}}
.lead{{color:#fff;font-weight:700}}
.rk{{display:inline-block;min-width:26px;text-align:center;font-size:10px;color:var(--muted);
border:1px solid var(--border);border-radius:999px;padding:1px 4px;margin:0 7px}}
.rk.lead{{color:#0b0d13;background:var(--accent);border-color:var(--accent);font-weight:700}}
ul{{margin:6px 0 0;padding-left:18px;color:var(--muted);font-size:12.5px}}
li{{margin:4px 0}}
em{{color:var(--text);font-style:normal;font-weight:600}}
.foot{{color:var(--muted);font-size:11px;margin-top:18px;border-top:1px solid var(--border);padding-top:10px}}
</style></head><body><div class="card">
<div class="hd"><h1>{away['name']} @ {home['name']}</h1>
<span class="meta">Advanced Matchup · {out.get('field_size','?')}-team field</span></div>
<span class="meta">{out.get('source','?')}{' · as of ' + out['as_of'][:19] if out.get('as_of') else ''}</span>
{sections}
<section><h2>Offense vs Defense Edges</h2><ul>{edges}</ul></section>
<div class="foot">Direction-aware: the leading side is highlighted per metric.
Polarity confirmed with Jeff before build. Stored analytics — no live API cost.</div>
</div></body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True)
    ap.add_argument("--away", required=True)
    ap.add_argument("--api", default=DEFAULT_API)
    ap.add_argument("--local", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--html", metavar="PATH")
    args = ap.parse_args()

    out = compute_local(args.away, args.home) if args.local else fetch_api(args.api, args.away, args.home)

    if args.html:
        Path(args.html).write_text(render_html(out), encoding="utf-8")
        print(f"html: {args.html}", file=sys.stderr)

    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print(render_text(out))
        if args.html:
            print(f"\n[html written to {args.html}]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
