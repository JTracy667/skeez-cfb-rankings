#!/usr/bin/env python3
"""atom_probe.py — did the OTHER analytics inputs move with today's ratings release?

Compares the live ETag of every endpoint we call against the ETag recorded in
data/api_surface_map.json (captured 2026-09-22). A 304 = unchanged since then;
a 200 = it has moved this week. This decides whether "pull on first ETag flip"
is safe, or whether datasets publish independently and a settle gate is needed.

Read-only. One conditional GET per endpoint.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

REPO = sys.argv[1] if len(sys.argv) > 1 else "."
BASE = "https://api.collegefootballdata.com"
GEORGIA = "Georgia"
YEAR = 2026


def api_key():
    for line in open(os.path.join(REPO, ".env"), encoding="utf-8"):
        if line.startswith("CFBD_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("no CFBD_API_KEY in repo .env")


KEY = api_key()

TARGETS = [
    ("teams", {}),
    ("ratings/sp", {}),
    ("ratings/elo", {}),
    ("ratings/fpi", {}),
    ("ratings/srs", {}),
    ("records", {}),
    ("recruiting/teams", {}),
    ("talent", {}),
    ("stats/season", {}),
    ("stats/season/advanced", {}),
    ("ppa/teams", {}),
    ("ppa/players/season", {}),
    ("player/returning", {}),
    ("roster", {"team": GEORGIA}),
    ("games/weather", {"week": 4, "team": GEORGIA}),
]


def cond_get(url, etag):
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", "Bearer " + KEY)
    req.add_header("User-Agent", "cfb-atom-probe/1.0")
    if etag:
        req.add_header("If-None-Match", etag)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            b = r.read()
            try:
                n = len(json.loads(b))
            except Exception:
                n = None
            return r.status, n
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return 304, None
        return e.code, None


def main():
    prev = json.load(open(os.path.join(REPO, "data", "api_surface_map.json"), encoding="utf-8"))
    changed, unchanged, unknown = [], [], []
    print(f"{'endpoint':24s} {'then':>5s} {'now':>6s}  verdict")
    print("-" * 62)
    for ep, params in TARGETS:
        rec = prev.get(ep) or {}
        etag = rec.get("etag")
        then_rows = rec.get("rows")
        q = {"year": YEAR}
        q.update(params)
        url = BASE + "/" + ep + "?" + urllib.parse.urlencode(q)
        st, now_rows = cond_get(url, etag)
        if st == 304:
            verdict, bucket = "unchanged since 9/22", unchanged
        elif st == 200:
            verdict, bucket = "CHANGED since 9/22", changed
        else:
            verdict, bucket = f"status {st}", unknown
        bucket.append(ep)
        print(f"{ep:24s} {str(then_rows):>5s} {str(now_rows):>6s}  {verdict}")
    print()
    print(f"CHANGED   ({len(changed)}): {', '.join(changed)}")
    print(f"unchanged ({len(unchanged)}): {', '.join(unchanged)}")
    if unknown:
        print(f"unknown   ({len(unknown)}): {', '.join(unknown)}")


if __name__ == "__main__":
    main()