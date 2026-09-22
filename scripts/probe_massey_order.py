#!/usr/bin/env python3
"""probe_massey_order.py — is the JSON usable at all?

Two questions, both cheap:
  1. DETERMINISM: fetch the same URL twice. Identical values => the payload is
     stable (usable). Different => it is salted per-request (obfuscated/session
     keyed, not usable as a plain feed).
  2. SANITY: does ANY column, sorted ascending, put the known-best FCS teams on
     top? The rendered table says the order is Montana St, Illinois St,
     SF Austin, S Dakota St, Lehigh... If no column reproduces even the head of
     that list, the numbers are not a ranking we can consume.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
CACHE = os.path.expandvars(
    r"%LOCALAPPDATA%\hermes\profiles\research\cache\web\masseyratings.com-2558703c7c01dd4d.cache.md")


def fetch(**params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(
        f"https://masseyratings.com/json/ranks.php?{q}",
        headers={"User-Agent": UA, "Referer": "https://masseyratings.com/ranks",
                 "X-Requested-With": "XMLHttpRequest", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def rendered_top(n=10):
    out = []
    for line in open(CACHE, encoding="utf-8", errors="replace"):
        if not line.startswith("| [") or "ranks?s=cf2026&t=" not in line:
            continue
        m = re.match(r"\[([^\]]+)\]", line.strip().strip("|").split("|")[0].strip())
        if m:
            out.append(m.group(1))
        if len(out) >= n:
            break
    return out


def main() -> int:
    a = fetch(s="cf", sub=11605)
    b = fetch(s="cf", sub=11605)

    def key(d):
        return {(r[0][0] if isinstance(r[0], list) else r[0]): [r[i] if not isinstance(r[i], list)
                else r[i][0] for i in range(4, len(r))] for r in d["DI"]}

    ka, kb = key(a), key(b)
    diff = [n for n in ka if ka[n] != kb.get(n)]
    print(f"1. DETERMINISM: {len(ka)} rows fetched twice")
    print(f"   rows differing between the two fetches: {len(diff)}")
    print("   ->", "STABLE (consumable)" if not diff else "SALTED / non-deterministic (NOT consumable)")
    if diff:
        n = diff[0]
        print(f"   e.g. {n}: {ka[n]}  vs  {kb[n]}")

    titles = [c.get("title") for c in a["CI"]]
    rows = a["DI"]
    print(f"\n2. SANITY vs the rendered order")
    print(f"   rendered top 10: {rendered_top(10)}")
    for i in range(4, len(titles)):
        vals = []
        for r in rows:
            nm = r[0][0] if isinstance(r[0], list) else r[0]
            v = r[i] if not isinstance(r[i], list) else r[i][0]
            if isinstance(v, (int, float)):
                vals.append((v, nm))
        vals.sort()
        print(f"   [{i}] {str(titles[i])[:9]:<9} asc: {[n for _, n in vals[:8]]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())