#!/usr/bin/env python3
"""probe_massey_rank.py — which JSON column, re-ranked WITHIN FCS, reproduces
the rendered composite rank?

Hypothesis: the JSON's numbers are on a NATIONAL pool (~1,100 teams, all
divisions), while the rendered table's CMP is the rank restricted to FCS (1..128).
If so, sorting the 128 FCS rows by the right column and assigning 1..128 should
reproduce the rendered CMP exactly.

Test both directions (ascending and descending) for every numeric column.
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


def parse_cached_table():
    out = {}
    for line in open(CACHE, encoding="utf-8", errors="replace"):
        if not line.startswith("| [") or "ranks?s=cf2026&t=" not in line:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        m = re.match(r"\[([^\]]+)\]", cells[0])
        if m:
            try:
                out[m.group(1)] = int(re.sub(r"[^0-9]", "", cells[4]) or 0)
            except Exception:
                pass
    return out


def main() -> int:
    cached = parse_cached_table()
    d = fetch(s="cf", sub=11605)
    rows, titles = d["DI"], [c.get("title") for c in d["CI"]]

    def num(r, i):
        v = r[i] if i < len(r) else None
        return v[0] if isinstance(v, list) else v

    teams = []
    for r in rows:
        nm = r[0][0] if isinstance(r[0], list) else r[0]
        if nm in cached:
            teams.append((nm, {i: num(r, i) for i in range(4, len(titles))}))
    print(f"matched {len(teams)} teams against the rendered table\n")

    print(f"  {'col':<12} {'dir':<5} {'exact':>8} {'off-by<=2':>10}  worst errors")
    best = None
    for i in range(4, len(titles)):
        vals = [(t[i], n) for n, t in teams if isinstance(t[i], (int, float))]
        if len(vals) < 100:
            continue
        for direction in ("asc", "desc"):
            ordered = sorted(vals, key=lambda x: x[0], reverse=(direction == "desc"))
            rank = {n: k + 1 for k, (_, n) in enumerate(ordered)}
            exact = sum(1 for _, n in vals if rank[n] == cached[n])
            close = sum(1 for _, n in vals if abs(rank[n] - cached[n]) <= 2)
            if best is None or exact > best[4]:
                best = (titles[i], direction, i, close, exact)
            if exact > 0 or close > 60:
                worst = sorted(((abs(rank[n] - cached[n]), n, rank[n], cached[n]) for _, n in vals),
                               reverse=True)[:4]
                print(f"  [{i}] {str(titles[i])[:9]:<9} {direction:<5} {exact:>4}/128 {close:>6}/128  "
                      + ", ".join(f"{n}:{r}vs{c}" for _, n, r, c in worst))

    print()
    if best and best[4] > 0:
        print(f"IDENTIFIED: column [{best[2]}] '{best[0]}' sorted {best[1]} — "
              f"{best[4]}/128 exact, {best[3]}/128 within 2")
    else:
        print("No column reproduces the rendered FCS composite by simple re-ranking.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())