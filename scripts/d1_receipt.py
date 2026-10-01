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

TABLES = ["teams", "players", "games", "stat_observations", "closing_lines",
          "odds_snapshots", "rankings_daily", "model_predictions", "raw_payloads"]


def _bootstrap_token() -> None:
    if os.environ.get("CF_D1_TOKEN") or os.environ.get("CLOUDFLARE_API_TOKEN"):
        return
    # No file fallback. A token was previously regex-harvested from
    # Desktop/Cloudflare.txt -- a plaintext credential on the desktop, which is exactly how a
    # run could pick up live D1 access without exporting anything.
    print("FATAL: no D1 token (set CF_D1_TOKEN, or CLOUDFLARE_API_TOKEN)", file=sys.stderr)
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
