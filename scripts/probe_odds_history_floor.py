#!/usr/bin/env python3
"""probe_odds_history_floor.py — how far back does The Odds API historical odds go?

The V4 harvest plan assumes OPEN (Mon) + CLOSE (Sat AM) snapshots over 5 seasons. That plan is
only valid if the archive reaches back to 2021. This probes one mid-season Saturday per year.

Each call costs 10 credits. Read-only.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = {}
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1)
        ENV[k.strip()] = v.strip().strip('"').strip("'")
KEY = ENV.get("THE_ODDS_API_KEY", "")
UA = "skeezcfb-rankings-probe/1.0"
OA = "https://api.the-odds-api.com/v4"

DATES = [
    ("2020-10-03", "2020 mid-season"),
    ("2021-09-04", "2021 week 1"),
    ("2021-10-02", "2021 mid-season"),
    ("2022-10-01", "2022 mid-season"),
    ("2023-10-07", "2023 mid-season"),
    ("2024-10-05", "2024 mid-season"),
    ("2025-10-04", "2025 mid-season"),
]


def main() -> int:
    rows = []
    for date, label in DATES:
        url = (f"{OA}/historical/sports/americanfootball_ncaaf/odds?apiKey={KEY}"
               f"&date={date}T18:00:00Z&regions=us&markets=spreads,totals&oddsFormat=american")
        rec = {"date": date, "label": label}
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.load(r)
                q = {k: v for k, v in r.headers.items() if "requests" in k.lower()}
            rec["status"] = 200
            rec["n_events"] = len(d.get("data", []))
            rec["quota"] = q
        except urllib.error.HTTPError as e:
            rec["status"] = e.code
            try:
                rec["body"] = json.loads(e.read())
            except Exception:  # noqa: BLE001
                pass
            rec["quota"] = {k: v for k, v in e.headers.items() if "requests" in k.lower()}
        except Exception as e:  # noqa: BLE001
            rec["status"] = type(e).__name__
        rows.append(rec)
        print(f"  {date} ({label:<18}) status={rec.get('status')} "
              f"events={rec.get('n_events')} {rec.get('body') or ''}")
        print(f"      quota={rec.get('quota')}")
    (ROOT / "data" / "odds_history_floor.json").write_text(json.dumps(rows, indent=2),
                                                           encoding="utf-8")
    ok = [r for r in rows if r.get("status") == 200 and (r.get("n_events") or 0) > 0]
    if ok:
        print(f"\n  EARLIEST with data: {ok[0]['date']} ({ok[0]['n_events']} events)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())