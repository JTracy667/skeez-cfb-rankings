#!/usr/bin/env python3
"""probe_odds_apis_detail.py — second pass: bookmakers, market/line structure, prop markets.

The first sweep answered "what endpoints exist". This answers "what is inside them":
  - the real bookmaker lists (sharp books matter: Pinnacle / Circa)
  - PropLine's per-book market + line fields (is open/close exposed?)
  - whether player props are reachable at all, and on which endpoint
Writes data/odds_api_detail.json. Read-only.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "odds_api_detail.json"

ENV = {}
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.strip().startswith("#"):
        k, v = line.split("=", 1)
        ENV[k.strip()] = v.strip().strip('"').strip("'")
PL_KEY, OA_KEY = ENV.get("PROPLINE_API_KEY", ""), ENV.get("THE_ODDS_API_KEY", "")
PL = "https://api.prop-line.com/v1"
OA = "https://api.the-odds-api.com/v4"
UA = "skeezcfb-rankings-probe/1.0 (+https://skeezcfb-rankings.com)"


def get(url, headers):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **headers})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.load(r), dict(r.headers)
    except urllib.error.HTTPError as e:
        try:
            return {"__error__": e.code, "body": json.loads(e.read())}, dict(e.headers)
        except Exception:  # noqa: BLE001
            return {"__error__": e.code}, dict(e.headers)


out = {}

# ---- The Odds API: sports keys, bookmakers, prop markets ------------------------------
sports, _ = get(f"{OA}/sports?apiKey={OA_KEY}&all=true", {})
if isinstance(sports, list):
    out["oa_football_sports"] = [{"key": s["key"], "title": s.get("title"),
                                  "active": s.get("active"), "has_outrights": s.get("has_outrights")}
                                 for s in sports if "football" in s["key"]]

odds, hdrs = get(f"{OA}/sports/americanfootball_ncaaf/odds?apiKey={OA_KEY}&regions=us,eu,uk"
                 f"&markets=spreads&oddsFormat=american", {})
books: dict = {}
mk: dict = {}
if isinstance(odds, list) and odds:
    for ev in odds:
        for b in ev.get("bookmakers", []):
            books.setdefault(b["key"], {"title": b.get("title"), "n_events": 0})
            books[b["key"]]["n_events"] += 1
            for m in b.get("markets", []):
                mk.setdefault(m["key"], 0)
                mk[m["key"]] += 1
    out["oa_odds_event_keys"] = sorted(odds[0].keys())
    out["oa_bookmaker_obj_keys"] = sorted((odds[0]["bookmakers"][0]).keys()) if odds[0].get("bookmakers") else []
    out["oa_market_obj_keys"] = sorted((odds[0]["bookmakers"][0]["markets"][0]).keys()) if odds[0].get("bookmakers") else []
    out["oa_outcome_obj_keys"] = sorted((odds[0]["bookmakers"][0]["markets"][0]["outcomes"][0]).keys()) if odds[0].get("bookmakers") else []
    out["oa_sample_bookmaker"] = json.dumps(odds[0]["bookmakers"][0])[:900]
out["oa_bookmakers"] = books
out["oa_markets_seen"] = mk
out["oa_quota"] = {k: v for k, v in hdrs.items() if "requests" in k.lower()}
out["oa_books_incl_sharp"] = [b for b in ("pinnacle", "circa", "bookmaker", "betcris", "lowvig",
                                          "betonlineag", "williamhill_us", "draftkings",
                                          "fanduel", "betmgm") if b in books]

# prop markets on the PER-EVENT endpoint (the /odds endpoint rejected them with 422)
ev_id = None
evs, _ = get(f"{OA}/sports/americanfootball_ncaaf/events?apiKey={OA_KEY}", {})
if isinstance(evs, list) and evs:
    ev_id = evs[0].get("id")
out["oa_prop_tests"] = {}
if ev_id:
    for mkt in ("player_pass_tds", "player_pass_yds", "player_rush_yds", "player_receptions",
                "team_totals", "alternate_spreads"):
        d, hh = get(f"{OA}/sports/americanfootball_ncaaf/events/{ev_id}/odds?apiKey={OA_KEY}"
                    f"&regions=us&markets={mkt}&oddsFormat=american", {})
        ok = isinstance(d, dict) and "bookmakers" in d
        out["oa_prop_tests"][mkt] = {
            "ok": ok,
            "error": (d.get("body", {}).get("message") if isinstance(d, dict) and "__error__" in d else None),
            "n_books": len(d.get("bookmakers", [])) if ok else 0,
            "markets_returned": sorted({mm["key"] for b in d.get("bookmakers", [])
                                        for mm in b.get("markets", [])}) if ok else [],
        }

# ---- PropLine: bookmakers + market/line structure -------------------------------------
podds, ph = get(f"{PL}/sports/football_ncaaf/odds?apiKey={PL_KEY}", {})
pbooks: dict = {}
pmk: dict = {}
if isinstance(podds, list) and podds:
    for ev in podds:
        for b in ev.get("bookmakers", []) or []:
            if isinstance(b, dict):
                pbooks.setdefault(b.get("key") or b.get("title"), 0)
                pbooks[b.get("key") or b.get("title")] += 1
                for m in (b.get("markets") or []) if isinstance(b.get("markets"), list) else []:
                    k = m.get("key") if isinstance(m, dict) else str(m)
                    pmk[k] = pmk.get(k, 0) + 1
    out["pl_event_keys"] = sorted(podds[0].keys())
    b0 = next((b for b in podds[0].get("bookmakers") or [] if isinstance(b, dict)), None)
    out["pl_bookmaker_sample"] = json.dumps(b0)[:1400] if b0 else None
    if b0:
        out["pl_bookmaker_keys"] = sorted(b0.keys())
        mm = b0.get("markets")
        if isinstance(mm, list) and mm:
            out["pl_market_keys"] = sorted(mm[0].keys()) if isinstance(mm[0], dict) else None
            out["pl_market_sample"] = json.dumps(mm[0])[:1200]
            m0 = mm[0]
            if isinstance(m0, dict):
                for cand in ("outcomes", "lines", "prices"):
                    if isinstance(m0.get(cand), list) and m0[cand]:
                        out[f"pl_{cand}_keys"] = sorted(m0[cand][0].keys()) if isinstance(m0[cand][0], dict) else None
                        out[f"pl_{cand}_sample"] = json.dumps(m0[cand][0])[:500]
out["pl_bookmakers"] = pbooks
out["pl_markets_seen"] = pmk
out["pl_quota"] = {k: v for k, v in ph.items() if "ratelimit" in k.lower()}

OUT.write_text(json.dumps(out, indent=2), encoding="utf-8")
print(f"wrote {OUT}")
print("\nOA football sports:", [s["key"] for s in out.get("oa_football_sports", [])])
print("OA bookmakers:", sorted(books))
print("OA sharp books present:", out["oa_books_incl_sharp"])
print("OA markets seen:", mk)
print("OA quota:", out["oa_quota"])
print("\nOA prop tests:")
for k, v in out["oa_prop_tests"].items():
    print(f"   {k:<20} ok={v['ok']} books={v['n_books']} markets={v['markets_returned']} err={v['error']}")
print("\nPL bookmakers:", sorted(pbooks))
print("PL markets seen:", pmk)
print("PL bookmaker keys:", out.get("pl_bookmaker_keys"))
print("PL market keys:", out.get("pl_market_keys"))
print("PL market sample:", out.get("pl_market_sample"))
print("PL quota:", out["pl_quota"])