"""Warm data/logo_cache from the D1 schedule payloads.

Logos are static images, but the card used to pull them from the CFBD CDN at RENDER time
-- a live external call inside the render path. `scripts/d1_board.logo_data_uri()` now
serves them from a local cache; this script fills that cache so a render needs only D1.

Run it once per machine (and again if new teams appear):

    CF_D1_TOKEN=... python scripts/warm_logo_cache.py

The cache is gitignored: it is derived data, and 237 PNGs of history is not worth it.
A missing logo degrades to a blank crest, never a failed card.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO))

import d1_board as B  # noqa: E402


def main() -> int:
    urls: set[str] = set()
    for week in range(1, 18):
        payload = B._slate_payload(week)
        if not payload:
            continue
        for m in payload.get("matchups") or []:
            for k in ("away_logo_url", "home_logo_url"):
                if m.get(k):
                    urls.add(m[k])
    print(f"distinct logo urls across the season: {len(urls)}")
    ok = miss = 0
    for u in sorted(urls):
        uri = B.logo_data_uri(u)
        if uri and "base64," in uri:
            ok += 1
        else:
            miss += 1
    have = len(list(B.LOGO_DIR.glob("*"))) if B.LOGO_DIR.exists() else 0
    print(f"cached ok={ok} failed={miss} | files in {B.LOGO_DIR}: {have}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
