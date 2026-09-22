#!/usr/bin/env python3
"""probe_api_surface.py — map the CFBD API's TRANSPORT surface, not its data.

We have always mapped the FIELDS we consume. We never mapped the operational surface:
which endpoints support conditional requests, what headers come back, and therefore
which datasets can be change-detected cheaply instead of blind-polled.

This is metadata reconnaissance only — request/response behaviour. It does NOT touch
what any source tracks or how parsers interpret it (that stays Research's territory).

For every endpoint the app actually calls, record:
  * status
  * ETag present? (and whether it is weak, W/"...")
  * Last-Modified / Cache-Control present?
  * Content-Length
  * X-CallLimit-Remaining (CFBD's own quota counter)
  * row count for a JSON array
  * CONDITIONAL: does If-None-Match on that ETag return 304?
  * COST: does that 304 still consume a call? (measured, not assumed)

Output: a table on stdout + data/api_surface_map.json for the record.

Read-only. Changes no serving path, writes no D1 rows.
Run:  python scripts/probe_api_surface.py
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(HERE, "data", "api_surface_map.json")
BASE = "https://api.collegefootballdata.com"
YEAR = 2026
GEORGIA = "Georgia"          # a stable team for endpoints that require one

# endpoint -> params (the endpoints cfbd_get() is actually called with)
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
    ("games", {}),
    ("lines", {"week": 4}),
    ("drives", {"week": 4, "team": GEORGIA}),
    ("roster", {"team": GEORGIA}),
    ("games/weather", {"week": 4, "team": GEORGIA}),
]


def api_key():
    k = os.environ.get("CFBD_API_KEY")
    if k:
        return k
    for line in open(os.path.join(HERE, ".env"), encoding="utf-8"):
        if line.startswith("CFBD_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("no CFBD_API_KEY")


KEY = api_key()


def build(endpoint, params):
    q = {"year": YEAR}
    q.update(params)
    return BASE + "/" + endpoint + "?" + urllib.parse.urlencode(q)


def call(url, etag=None):
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", "Bearer " + KEY)
    if etag:
        req.add_header("If-None-Match", etag)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read()
            return {
                "status": r.status, "etag": r.headers.get("ETag"),
                "last_modified": r.headers.get("Last-Modified"),
                "cache_control": r.headers.get("Cache-Control"),
                "length": r.headers.get("Content-Length"),
                "remaining": r.headers.get("X-CallLimit-Remaining"),
                "body": body,
            }
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return {"status": 304, "etag": e.headers.get("ETag"),
                    "remaining": e.headers.get("X-CallLimit-Remaining"), "body": b""}
        return {"status": e.code, "error": str(e)[:120], "body": b""}
    except Exception as e:
        return {"status": None, "error": str(e)[:120], "body": b""}


def rows(body):
    try:
        d = json.loads(body)
        return len(d) if isinstance(d, list) else 1
    except Exception:
        return None


def main():
    import urllib.parse  # noqa: F401  (used by build via module global)
    results = {}
    print(f"{'endpoint':24s} {'st':>3s} {'ETag':>5s} {'LastMod':>8s} "
          f"{'Cache':>6s} {'bytes':>8s} {'rows':>5s} {'304?':>5s} {'304cost':>8s}")
    print("-" * 84)
    for endpoint, params in TARGETS:
        url = build(endpoint, params)
        first = call(url)
        etag = first.get("etag")
        rec = {
            "status": first.get("status"), "etag": etag,
            "etag_weak": bool(etag and etag.startswith('W/')),
            "last_modified": first.get("last_modified"),
            "cache_control": first.get("cache_control"),
            "bytes": int(first["length"]) if (first.get("length") or "").isdigit() else len(first.get("body") or b""),
            "rows": rows(first.get("body") or b""),
            "remaining_after_first": first.get("remaining"),
            "error": first.get("error"),
        }
        # conditional probe + quota delta
        if etag:
            r_before = first.get("remaining")
            second = call(url, etag=etag)
            rec["conditional_status"] = second.get("status")
            rec["remaining_after_conditional"] = second.get("remaining")
            try:
                rec["conditional_costs_a_call"] = int(r_before) - int(second.get("remaining"))
            except Exception:
                rec["conditional_costs_a_call"] = None
        results[endpoint] = rec
        print(f"{endpoint:24s} {str(rec['status']):>3s} "
              f"{('yes' if etag else 'no'):>5s} "
              f"{('yes' if rec['last_modified'] else 'no'):>8s} "
              f"{('yes' if rec['cache_control'] else 'no'):>6s} "
              f"{str(rec['bytes']):>8s} {str(rec['rows']):>5s} "
              f"{str(rec.get('conditional_status', '-')):>5s} "
              f"{str(rec.get('conditional_costs_a_call', '-')):>8s}")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, sort_keys=True, default=str)

    yes = [e for e, r in results.items() if r["etag"]]
    cond = [e for e, r in results.items() if r.get("conditional_status") == 304]
    print()
    print(f"ETag support   : {len(yes)}/{len(results)} endpoints")
    print(f"304 on If-None-Match: {len(cond)}/{len(yes)} of those")
    print(f"map written to : {OUT}")
    return 0


if __name__ == "__main__":
    u = urllib.parse  # ensure the module is bound before main() uses it
    sys.exit(main())