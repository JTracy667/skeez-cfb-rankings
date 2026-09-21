#!/usr/bin/env python3
"""d1_receipt.py — print live D1 row counts (the receipt the CEO asks for).

Reads CF_D1_TOKEN from env, else the local dev token file (never printed).
Usage:  python scripts/d1_receipt.py [--json]
"""
from __future__ import annotations

import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import d1_store  # noqa: E402

TABLES = ["teams", "games", "stat_observations", "closing_lines",
          "rankings_daily", "odds_snapshots", "model_predictions",
          "raw_payloads", "stat_map"]


def _bootstrap_token() -> None:
    if os.environ.get("CF_D1_TOKEN") or os.environ.get("CLOUDFLARE_API_TOKEN"):
        return
    for p in (os.path.join(os.environ.get("USERPROFILE", ""), "Desktop", "Cloudflare.txt"),
              os.path.join(os.path.expanduser("~"), "Desktop", "Cloudflare.txt")):
        try:
            m = re.search(r"cfat_[A-Za-z0-9_\-]+", open(p, encoding="utf-8", errors="ignore").read())
            if m:
                os.environ["CF_D1_TOKEN"] = m.group(0)
                return
        except OSError:
            continue
    print("FATAL: no D1 token (set CF_D1_TOKEN)", file=sys.stderr)
    raise SystemExit(2)


def main() -> int:
    _bootstrap_token()
    out = {}
    for t in TABLES:
        try:
            out[t] = d1_store.query(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"]
        except Exception as e:  # noqa: BLE001
            out[t] = f"ERR {e}"
    out["_ledger_written_local"] = d1_store.ledger_written()
    if "--json" in sys.argv:
        print(json.dumps(out, indent=2))
    else:
        for k, v in out.items():
            print(f"{k:24s} {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
