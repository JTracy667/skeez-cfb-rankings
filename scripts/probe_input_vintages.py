"""Probe: is each composite input actually CURRENT this season, or a season old?

The composite is fed by ~14 CFBD endpoints. Several of them fall back to the PREVIOUS
season when the current season returns nothing (see the CFBD_YEAR / CFBD_YEAR_FALLBACK
pairs in app.py), and that fallback is silent — which is how SRS carried 12% of the
composite weight on last season's ratings without anyone noticing.

This asks each endpoint directly for the current season and reports the row count, so
"which inputs are stale" is measured rather than assumed. Re-run any time; it is the
regenerable source for docs/CFBD_COMPOSITE_INPUTS.md.

Usage:  python scripts/probe_input_vintages.py [--year 2026] [--json path]
"""
import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

BASE = "https://api.collegefootballdata.com"

# (label, endpoint, composite input it feeds)
ENDPOINTS = [
    ("SP+",                  "ratings/sp",              "sp_plus  (18% of the composite)"),
    ("FPI",                  "ratings/fpi",             "fpi      (15%)"),
    ("SRS",                  "ratings/srs",             "srs      (12%)"),
    ("Elo",                  "ratings/elo",             "elo       (8%)"),
    ("Talent (247)",         "talent",                  "talent   (10%)"),
    ("Recruiting",           "recruiting/teams",        "talent  (30% of the talent prior)"),
    ("Season stats",         "stats/season",            "efficiency (net success rate)"),
    ("Advanced stats",       "stats/season/advanced",   "efficiency (trench: line yards/stuff)"),
    ("PPA/teams",            "ppa/teams",               "efficiency (net EPA/PPA per play)"),
    ("Drives",               "drives",                  "efficiency (points per possession)"),
    ("Player returning",     "player/returning",        "talent   (returning production)"),
    ("Roster",               "roster",                  "talent   (experience)"),
    ("Records",              "records",                 "W/L overlay"),
    ("Teams",                "teams",                   "name/ID mapping"),
]


def key():
    k = os.environ.get("CFBD_API_KEY")
    if not k:
        env = Path(__file__).resolve().parent.parent / ".env"
        if env.exists():
            for line in env.read_text().splitlines():
                if line.startswith("CFBD_API_KEY="):
                    k = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    return k


def fetch(endpoint, year, k):
    url = f"{BASE}/{endpoint}?year={year}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {k}",
        "Accept": "application/json",
        "User-Agent": "skeez-cfb-input-vintage-probe/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read()
            remain = r.headers.get("X-CallLimit-Remaining")
            etag = r.headers.get("ETag")
    except Exception as e:
        return None, None, None, str(e)
    try:
        data = json.loads(raw)
    except Exception:
        return None, remain, etag, "unparseable body"
    n = len(data) if isinstance(data, list) else (1 if data else 0)
    return n, remain, etag, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=None)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    k = key()
    if not k:
        print("  no CFBD_API_KEY (env or repo .env)")
        return 1

    year = args.year or 2026
    print(f"Composite input freshness — is {year} populated, or is the app on a fallback?\n")
    print(f"  {'input':<22} {'endpoint':<24} {year:>7}  {'prev yr':>7}  verdict")
    print("  " + "-" * 82)

    rows = []
    stale = []
    for label, endpoint, feeds in ENDPOINTS:
        n, remain, etag, err = fetch(endpoint, year, k)
        if err:
            print(f"  {label:<22} {endpoint:<24} {'ERR':>7}  {err[:28]}")
            rows.append({"label": label, "endpoint": endpoint, "feeds": feeds,
                         "rows": None, "error": err})
            continue
        prev = None
        verdict = "CURRENT"
        if n == 0:
            prev, _, _, _ = fetch(endpoint, year - 1, k)
            verdict = "STALE -> previous season" if prev else "empty both years"
            stale.append((label, endpoint, feeds, prev))
        print(f"  {label:<22} {endpoint:<24} {n:>7}  {str(prev if prev is not None else '-'):>7}  {verdict}")
        rows.append({"label": label, "endpoint": endpoint, "feeds": feeds,
                     "rows_current": n, "rows_previous": prev, "verdict": verdict,
                     "etag": etag})

    print()
    if stale:
        print("  Inputs the app is serving from the PREVIOUS season:")
        for label, endpoint, feeds, prev in stale:
            print(f"    - {label:<20} {endpoint:<22} feeds {feeds}")
    else:
        print("  Every composite input is current this season.")

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"year": year, "checked_utc": __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc).isoformat(timespec="seconds"),
             "inputs": rows}, indent=2))
        print(f"\n  written: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())