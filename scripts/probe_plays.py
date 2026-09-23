#!/usr/bin/env python3
"""probe_plays.py — ARM 2 gate: size the CFBD /plays pull with ONE call before committing.

The V5 work order says /plays is not yet in the API map (verified: no mention in
docs/CFBD_API_MAP.md) and that the monthly CFBD budget is nearly spent, so the harvest is
gated on a single measured probe rather than an assumption:

    python scripts/probe_plays.py --season 2024 --week 8      # one call, prints size/rows
    python scripts/probe_plays.py --season 2024 --week 8 --team "Alabama"   # posteam filter

Reports: HTTP status, payload bytes, play rows, the JSON field set, and the server's own
rate-limit headers when present. Extrapolates a 5-season row count from measured weeks.
Also records the CFBD request/remaining counters from the response headers, because the
quota number matters more than the row estimate.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
CFBD_BASE = "https://api.collegefootballdata.com"


def key() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("CFBD_API_KEY"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("CFBD_API_KEY not found in .env")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=2024)
    ap.add_argument("--week", type=int, default=8)
    ap.add_argument("--team", default=None)
    ap.add_argument("--saveto", default=None, help="write the raw response here for inspection")
    args = ap.parse_args()

    q = f"year={args.season}&week={args.week}"
    if args.team:
        q += f"&team={urllib.parse.quote(args.team)}"
    url = f"{CFBD_BASE}/plays?{q}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {key()}", "User-Agent": "cfb-analytics/1.0"})
    print(f"[plays] GET {url}")
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            raw = r.read()
            hdrs = dict(r.headers)
            status = r.status
    except urllib.error.HTTPError as e:
        print(f"[plays] HTTP {e.code}: {e.read()[:300]!r}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"[plays] error: {e}")
        return 1

    data = json.loads(raw.decode("utf-8", "replace"))
    print(f"[plays] HTTP {status}  bytes {len(raw):,}  rows {len(data):,}")
    for h in ("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-used",
              "x-calls-remaining", "retry-after"):
        if h in hdrs:
            print(f"[plays] header {h}: {hdrs[h]}")
    if data:
        print(f"[plays] fields: {sorted(data[0].keys())}")
        print(f"[plays] sample: {json.dumps(data[0], default=str)[:400]}")
    if args.saveto:
        Path(args.saveto).write_bytes(raw)
        print(f"[plays] saved -> {args.saveto}")

    # Extrapolation: ~15 weeks x 5 seasons, scaled by the measured week
    per_week = len(data)
    print(f"[plays] extrapolation: {per_week:,} rows/week x ~15 weeks x 5 seasons "
          f"= {per_week * 15 * 5:,} rows; payload ~{len(raw) * 15 * 5 / 1e6:.1f} MB "
          f"(before any team filter)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())