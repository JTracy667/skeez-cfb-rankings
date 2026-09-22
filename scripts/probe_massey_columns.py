#!/usr/bin/env python3
"""probe_massey_columns.py — identify which JSON column IS the composite rank.

The endpoint's column titles (CMP, Consensus, Spread, Min, Max, Median) cannot be
trusted positionally until proven: the JSON's CMP ranges 77..1106 while the
RENDERED table's CMP for the same teams is 1..128. One of the trailing numeric
columns must be the real rank. Rather than eyeballing it, score every candidate
column against the rendered table for all 128 teams — exact-match count plus a
rank correlation. The winner is the column that agrees 128/128.
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


def fetch(**params) -> dict:
    q = "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(
        f"https://masseyratings.com/json/ranks.php?{q}",
        headers={"User-Agent": UA, "Referer": "https://masseyratings.com/ranks",
                 "X-Requested-With": "XMLHttpRequest", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def parse_cached_table():
    out = {}
    if not os.path.exists(CACHE):
        return out
    for line in open(CACHE, encoding="utf-8", errors="replace"):
        if not line.startswith("| [") or "ranks?s=cf2026&t=" not in line:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        m = re.match(r"\[([^\]]+)\]", cells[0])
        if not m:
            continue
        try:
            out[m.group(1)] = int(re.sub(r"[^0-9]", "", cells[4]) or 0)
        except Exception:
            pass
    return out


def spearman(pairs):
    """Spearman rho on (a,b) pairs — rank-transform then Pearson."""
    n = len(pairs)
    if n < 3:
        return 0.0

    def ranks(vals):
        order = sorted(range(n), key=lambda i: vals[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    ra, rb = ranks([p[0] for p in pairs]), ranks([p[1] for p in pairs])
    ma, mb = sum(ra) / n, sum(rb) / n
    num = sum((ra[i] - ma) * (rb[i] - mb) for i in range(n))
    da = sum((x - ma) ** 2 for x in ra) ** 0.5
    db = sum((x - mb) ** 2 for x in rb) ** 0.5
    return num / (da * db) if da and db else 0.0


def main() -> int:
    cached = parse_cached_table()
    print(f"rendered table: {len(cached)} teams (authoritative FCS composite rank)\n")

    for label, params in (("default  s=cf&sub=11605", {"s": "cf", "sub": 11605}),
                          ("consensus top=-9", {"s": "cf", "sub": 11605, "top": -9})):
        d = fetch(**params)
        rows = d["DI"]
        titles = [c.get("title") for c in d["CI"]]
        print("=" * 74)
        print(f"{label}   rows={len(rows)}")
        print("titles:", titles)
        byname = {}
        for r in rows:
            nm = r[0][0] if isinstance(r[0], list) else r[0]
            byname[nm] = r

        def num(r, i):
            v = r[i] if i < len(r) else None
            return v[0] if isinstance(v, list) else v

        print(f"  {'col':<14} {'exact==rendered':>16} {'spearman':>9}")
        best = (None, -1, 0)
        for i in range(4, len(titles)):
            pairs, exact = [], 0
            for nm, cval in cached.items():
                r = byname.get(nm)
                if not r:
                    continue
                v = num(r, i)
                if isinstance(v, (int, float)):
                    pairs.append((v, cval))
                    if abs(v - cval) < 1e-9:
                        exact += 1
            rho = spearman(pairs)
            print(f"  [{i}] {str(titles[i])[:10]:<11} {exact:>10}/{len(cached):<5} {rho:>9.3f}")
            if exact > best[1]:
                best = (titles[i], exact, i)
        print(f"  -> best: column [{best[2]}] {best[0]} with {best[1]}/{len(cached)} exact\n")

        if best[2] is not None:
            print(f"  sanity: the {best[1]} matched teams' values, sorted ascending, first 12:")
            vals = []
            for nm, r in byname.items():
                v = num(r, best[2])
                if isinstance(v, (int, float)):
                    vals.append((v, nm))
            for v, nm in sorted(vals)[:12]:
                print(f"     {v:>6}  {nm}")

        ms = byname.get("Montana St")
        if ms:
            print(f"\n  Montana St full row: {json.dumps(ms)}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())