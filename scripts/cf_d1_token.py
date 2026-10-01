"""Print the working D1 token, verifying it against D1 first.

Mirrors scripts/cf_deploy_token.py: prints the value on stdout so a shell can capture it

    CF_D1_TOKEN=$(python scripts/cf_d1_token.py) python scripts/d1_receipt.py

and exits non-zero with a clear message if it cannot find one that works. The value is NEVER
written into a repo file, a commit, or a chat message -- the only copy lives in
profiles/cto/.cf_d1_token, which is outside the repo.

Why a file at all: the D1 scripts need a credential and there is no secret manager for local
scripts. That file is the same deliberate, single-copy pattern as .cf_deploy_token -- and unlike
the Desktop file it replaced, nothing harvests it by regex.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

ACCOUNT_ID = "90c2c31beec12cb7de1c249ade1eb773"
CANDIDATES = (
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "hermes", "profiles", "cto", ".cf_d1_token"),
)


def _works(tok: str) -> bool:
    """A read-only call: listing databases proves D1 access without touching any data."""
    req = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/d1/database",
        headers={"Authorization": f"Bearer {tok}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return bool(json.loads(r.read()).get("success"))
    except urllib.error.HTTPError:
        return False
    except OSError:
        return False


def main() -> int:
    env = os.environ.get("CF_D1_TOKEN", "").strip()
    if env and _works(env):
        print(env)
        return 0
    for p in CANDIDATES:
        try:
            tok = open(p, encoding="utf-8").read().strip()
        except OSError:
            continue
        if tok and _works(tok):
            print(tok)
            return 0
        print(f"WARNING: the token in {p} does not work against D1 (401) -- roll it: "
              "create a D1 Read+Write account token and replace that file.", file=sys.stderr)
    print("FATAL: no working D1 token. Create one (D1 Read + D1 Write) and save it to "
          f"{CANDIDATES[0]}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())