#!/usr/bin/env python3
"""probe_odds_apis.py — read-only capability sweep of PropLine + The Odds API.

Methodology follows scripts/probe_api_surface.py: read-only GETs, every result live-verified,
schema recorded rather than assumed. Writes data/odds_api_surface.json.

Budget discipline: PropLine < 1000 calls (5K/day), The Odds API < 500 credits (20K). One call per
probe endpoint, plus a handful of market variations. Keys are never printed.

Verdict vocabulary (matches docs/CFBD_API_MAP.md):
  VERIFIED-LIVE  HTTP 200 with a non-empty body
  EMPTY          200/404 with an empty or null body
  JUNK           error, 4xx that is not 404, or a body we cannot use
  UNPROBED       not attempted (say why)
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "odds_api_surface.json"

ENV = {}
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1)
        ENV[k.strip()] = v.strip().strip('"').strip("'")

PL_KEY = ENV.get("PROPLINE_API_KEY", "")
OA_KEY = ENV.get("THE_ODDS_API_KEY", "")
PL = "https://api.prop-line.com/v1"
OA = "https://api.the-odds-api.com/v4"
UA = "skeezcfb-rankings-probe/1.0 (+https://skeezcfb-rankings.com)"

PL_HEADERS = ["x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset",
              "x-ratelimit-used", "ratelimit-limit", "ratelimit-remaining",
              "x-quota-limit", "x-quota-remaining", "x-quota-reset"]
OA_HEADERS = ["x-requests-remaining", "x-requests-used", "x-requests-last"]


def mask(u: str) -> str:
    return u.replace(PL_KEY, "***").replace(OA_KEY, "***") if (PL_KEY or OA_KEY) else u


def schema(v, depth=0, max_depth=3):
    """Compact structural summary of a JSON value."""
    if depth >= max_depth:
        return type(v).__name__
    if isinstance(v, dict):
        return {k: schema(val, depth + 1, max_depth) for k, val in list(v.items())[:40]}
    if isinstance(v, list):
        return [schema(v[0], depth + 1, max_depth)] if v else []
    return type(v).__name__


def probe(name, url, headers, quota_headers, params=None):
    if params:
        q = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{q}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, **headers})
    rec = {"name": name, "url": mask(url), "verdict": None}
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            body = r.read()
            rec["status"] = r.status
            rec["bytes"] = len(body)
            rec["quota"] = {h: r.headers.get(h) for h in quota_headers if r.headers.get(h)}
            try:
                data = json.loads(body)
            except Exception:  # noqa: BLE001
                rec["verdict"] = "JUNK"
                rec["note"] = "body is not JSON"
                rec["body_head"] = body[:200].decode("utf-8", "replace")
                return rec
            rec["schema"] = schema(data)
            if isinstance(data, list):
                rec["n"] = len(data)
                rec["verdict"] = "VERIFIED-LIVE" if data else "EMPTY"
                if data and isinstance(data[0], dict):
                    rec["sample_keys"] = sorted(data[0].keys())
                    rec["sample"] = {k: data[0][k] for k in list(data[0])[:22]}
                    if len(data) > 1 and isinstance(data[1], dict):
                        rec["extra_keys_union"] = sorted(
                            set().union(*[set(d.keys()) for d in data[:50] if isinstance(d, dict)]))
            elif isinstance(data, dict):
                rec["keys"] = sorted(data.keys())
                any_list = next((k for k, v in data.items() if isinstance(v, list) and v), None)
                rec["n"] = len(data[any_list]) if any_list else None
                rec["verdict"] = "VERIFIED-LIVE" if data else "EMPTY"
                rec["sample"] = {k: (data[k][:1] if isinstance(data[k], list) else data[k])
                                 for k in list(data)[:14]}
            else:
                rec["verdict"] = "JUNK"
    except urllib.error.HTTPError as e:
        rec["status"] = e.code
        rec["verdict"] = "EMPTY" if e.code == 404 else "JUNK"
        try:
            rec["error_body"] = e.read()[:240].decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        for h in quota_headers:
            if e.headers.get(h):
                rec.setdefault("quota", {})[h] = e.headers.get(h)
    except Exception as e:  # noqa: BLE001
        rec["verdict"] = "JUNK"
        rec["note"] = f"{type(e).__name__}: {e}"
    return rec


def main() -> int:
    if not PL_KEY or not OA_KEY:
        print("missing keys in .env")
        return 1
    results = {"propline": [], "oddsapi": []}

    ph = {"Authorization": f"Bearer {PL_KEY}", "X-Api-Key": PL_KEY}
    oh = {"Accept": "application/json"}

    # ---------------- PROP LINE ----------------
    P = [
        ("sports list", f"{PL}/sports", {"apiKey": PL_KEY}),
        ("leagues list", f"{PL}/leagues", {"apiKey": PL_KEY}),
        ("ncaaf odds (spreads,totals)", f"{PL}/sports/football_ncaaf/odds",
         {"apiKey": PL_KEY, "markets": "spreads,totals"}),
        ("ncaaf odds (moneyline)", f"{PL}/sports/football_ncaaf/odds",
         {"apiKey": PL_KEY, "markets": "moneyline"}),
        ("ncaaf odds (team_totals)", f"{PL}/sports/football_ncaaf/odds",
         {"apiKey": PL_KEY, "markets": "team_totals"}),
        ("ncaaf odds (all markets)", f"{PL}/sports/football_ncaaf/odds", {"apiKey": PL_KEY}),
        ("ncaaf events", f"{PL}/sports/football_ncaaf/events", {"apiKey": PL_KEY}),
        ("ncaaf events (no key)", f"{PL}/sports/football_ncaaf/events", None),
        ("ncaaf odds/history", f"{PL}/sports/football_ncaaf/odds/history", {"apiKey": PL_KEY}),
        ("ncaaf odds history qs", f"{PL}/sports/football_ncaaf/history", {"apiKey": PL_KEY}),
        ("books list", f"{PL}/books", {"apiKey": PL_KEY}),
        ("bookmakers", f"{PL}/bookmakers", {"apiKey": PL_KEY}),
        ("nfl odds (other league)", f"{PL}/sports/football_nfl/odds",
         {"apiKey": PL_KEY, "markets": "spreads"}),
        ("player props (ncaaf)", f"{PL}/sports/football_ncaaf/odds",
         {"apiKey": PL_KEY, "markets": "player_props"}),
    ]
    for name, url, q in P:
        results["propline"].append(probe(name, url, ph, PL_HEADERS, q))

    # ---------------- THE ODDS API ----------------
    O = [
        ("sports list", f"{OA}/sports", {"apiKey": OA_KEY}),
        ("sports (all, incl. outrights)", f"{OA}/sports", {"apiKey": OA_KEY, "all": "true"}),
        ("ncaaf events", f"{OA}/sports/americanfootball_ncaaf/events", {"apiKey": OA_KEY}),
        ("ncaaf odds h2h+spreads+totals", f"{OA}/sports/americanfootball_ncaaf/odds",
         {"apiKey": OA_KEY, "regions": "us", "markets": "h2h,spreads,totals", "oddsFormat": "american"}),
        ("ncaaf odds (us,eu,uk books)", f"{OA}/sports/americanfootball_ncaaf/odds",
         {"apiKey": OA_KEY, "regions": "us,eu,uk", "markets": "spreads", "oddsFormat": "american"}),
        ("ncaaf scores", f"{OA}/sports/americanfootball_ncaaf/scores",
         {"apiKey": OA_KEY, "daysFrom": "3"}),
        ("HISTORICAL ncaaf odds", f"{OA}/historical/sports/americanfootball_ncaaf/odds",
         {"apiKey": OA_KEY, "date": "2025-10-01T00:00:00Z", "regions": "us",
          "markets": "spreads", "oddsFormat": "american"}),
        ("ncaaf player props (attempt)", f"{OA}/sports/americanfootball_ncaaf/odds",
         {"apiKey": OA_KEY, "regions": "us", "markets": "player_pass_yds", "oddsFormat": "american"}),
        ("ncaaf event odds (needs id)", f"{OA}/sports/americanfootball_ncaaf/events/PLACEHOLDER/odds",
         {"apiKey": OA_KEY, "regions": "us", "markets": "spreads"}),
    ]
    for name, url, q in O:
        results["oddsapi"].append(probe(name, url, oh, OA_HEADERS, q))

    # follow-up: real per-event odds using a discovered event id
    for r in results["oddsapi"]:
        if r.get("name") == "ncaaf events" and r.get("n"):
            eid = None
            try:
                rr = urllib.request.Request(
                    f"{OA}/sports/americanfootball_ncaaf/events?apiKey={OA_KEY}",
                    headers={"User-Agent": UA})
                with urllib.request.urlopen(rr, timeout=45) as resp:
                    eid = json.load(resp)[0].get("id")
            except Exception:  # noqa: BLE001
                pass
            if eid:
                results["oddsapi"].append(probe(
                    "ncaaf event odds (real id)",
                    f"{OA}/sports/americanfootball_ncaaf/events/{eid}/odds",
                    oh, OA_HEADERS,
                    {"apiKey": OA_KEY, "regions": "us", "markets": "spreads,team_totals",
                     "oddsFormat": "american"}))
            break

    OUT.write_text(json.dumps(results, indent=2), encoding="utf-8")
    for api, rows in results.items():
        print(f"\n=== {api.upper()} ({len(rows)} probes) ===")
        for r in rows:
            n = r.get("n")
            q = r.get("quota") or {}
            print(f"  {r['verdict']:<14} {r['name'][:38]:<38} status={r.get('status')} "
                  f"n={n} bytes={r.get('bytes')} {list(q.items())[:2]}")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())