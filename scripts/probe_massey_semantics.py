#!/usr/bin/env python3
"""probe_massey_semantics.py — pin down the SCALE of the Massey FCS JSON.

Before any parser is written we must know what the numbers MEAN. The 10 columns
the endpoint advertises are: Team, Conf, W-L, Change, CMP, Consensus, Spread,
Min, Max, Median. Min/Max/Median/CMP look like ranks; Consensus looked absurd
for a 128-row table, which is the thing to resolve.

Method: the endpoint is compared against Research's RENDERED copy of the same
page from 2026-09-21 (a real browser render we already have). Same date => the
two must agree team-for-team, which settles the scale definitively.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
BASE = "https://masseyratings.com/json/ranks.php"
CACHE = os.path.expandvars(
    r"%LOCALAPPDATA%\hermes\profiles\research\cache\web\masseyratings.com-2558703c7c01dd4d.cache.md")


def fetch(**params) -> dict:
    q = "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(
        f"{BASE}?{q}",
        headers={"User-Agent": UA, "Referer": "https://masseyratings.com/ranks",
                 "X-Requested-With": "XMLHttpRequest", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def parse_cached_table():
    """Pull (team, cmp, delta) out of the rendered markdown table."""
    if not os.path.exists(CACHE):
        return {}
    out = {}
    for line in open(CACHE, encoding="utf-8", errors="replace"):
        if not line.startswith("| [") or "ranks?s=cf2026&t=" not in line:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        m = re.match(r"\[([^\]]+)\]", cells[0])
        if not m:
            continue
        team = m.group(1)
        # columns: Team, Conf, W-L, Δ, CMP, Sort, then raters...
        try:
            cmp_ = int(re.sub(r"[^0-9]", "", cells[4]) or 0)
        except Exception:
            cmp_ = None
        out[team] = cmp_
    return out


def main() -> int:
    print("=" * 72)
    print("A. default view  (s=cf, sub=11605)")
    print("=" * 72)
    d = fetch(s="cf", sub=11605)
    rows, ci = d["DI"], d["CI"]
    print("columns:", [c.get("title") for c in ci])
    print("rows:", len(rows), " seas:", d.get("seas"), " title:", d.get("title"))

    def col(i):
        out = []
        for r in rows:
            v = r[i] if i < len(r) else None
            out.append(v[0] if isinstance(v, list) else v)
        return out

    for name, i in (("CMP", 4), ("Consensus", 5), ("Spread", 6), ("Min", 7), ("Max", 8), ("Median", 9)):
        vals = [v for v in col(i) if isinstance(v, (int, float))]
        if vals:
            print(f"  {name:<10} n={len(vals):>4} min={min(vals):<12} max={max(vals):<12} "
                  f"median={sorted(vals)[len(vals)//2]:<12} sample={vals[:6]}")
        else:
            print(f"  {name:<10} no numeric values")

    print("\n  first 6 rows:")
    for r in rows[:6]:
        print("   ", json.dumps(r)[:170])

    print()
    print("=" * 72)
    print("B. the consensus view that Research rendered (top=-9)")
    print("=" * 72)
    try:
        d2 = fetch(s="cf", sub=11605, top=-9)
        print("columns:", [c.get("title") for c in d2["CI"]])
        print("rows:", len(d2["DI"]))
        for r in d2["DI"][:4]:
            print("   ", json.dumps(r)[:200])
    except Exception as e:
        print("  failed:", e)

    print()
    print("=" * 72)
    print("C. cross-check: JSON vs the rendered table (same date)")
    print("=" * 72)
    cached = parse_cached_table()
    print("rendered rows parsed:", len(cached))
    byname = {}
    for r in rows:
        nm = r[0][0] if isinstance(r[0], list) else r[0]
        byname[nm] = r[4] if not isinstance(r[4], list) else r[4][0]
    print(f"  {'team':<18} {'json CMP':>9} {'rendered CMP':>13}")
    for nm in list(cached)[:12]:
        print(f"  {nm:<18} {str(byname.get(nm)):>9} {str(cached.get(nm)):>13}")
    return 0


if __name__ == "__main__":
    sys.exit(main())