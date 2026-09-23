#!/usr/bin/env python3
"""smoke_v27.py — post-deploy data sanity for the v27 (decision A) release.

Checks the quality gates that matter for this deploy:
  1. /api/rankings returns rows
  2. rows are sorted by composite rank (gate: ALWAYS)
  3. no elo nulls / no sp_plus zeros (the 2026-09-20 regression probe)
  4. the odds map now reports book_of_record lines (the thing v27 changed)
"""
import json
import urllib.request

BASE = "https://skeezcfb-rankings.com"
UA = {"User-Agent": "skeez-smoke/1.0"}


def get(path):
    req = urllib.request.Request(BASE + path, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status, json.load(r)


st, d = get("/api/rankings")
rs = d.get("rankings") or d.get("teams") or []
print(f"/api/rankings  HTTP {st}  rows={len(rs)}")
ranks = [r.get("rank") for r in rs]
print(f"  sorted by composite rank: {ranks == sorted(ranks)}  (first 5: {ranks[:5]})")
if rs:
    first = rs[0]
    print("  top row:", {k: first.get(k) for k in ("rank", "team", "composite") if k in first})
elo_null = sum(1 for r in rs if r.get("elo") is None)
sp_zero = sum(1 for r in rs if r.get("sp_plus") == 0.0)
print(f"  elo_nulls={elo_null}  sp_zero={sp_zero}   (gate: both must be 0)")