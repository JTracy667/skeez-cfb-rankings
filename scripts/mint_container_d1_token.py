"""Mint a D1 read+write token for the PRODUCTION container, for the Worker secret CF_D1_TOKEN.

Why: after the 2026-10-01 recycle the container lost D1 access entirely (no writes since 03:34Z,
analytics serving its baked disk copy with degraded=true). The token roll on 2026-09-30 is the
prime suspect. This mints a dedicated, named credential so the container's access is attributable
and independently revocable.

The VALUE IS NEVER PRINTED: it goes to profiles/cto/.cf_container_d1_token, and this reports a
sha256[:12] fingerprint plus the id. That is the steep-river-d24c lesson -- a credential that
passes through stdout ends up in agent.log/gateway.log in plaintext.

    python scripts/mint_container_d1_token.py
"""
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

ACCT = "90c2c31beec12cb7de1c249ade1eb773"
D1_READ = "192192df92ee43ac90f2aeeffce67e35"
D1_WRITE = "09b2857d1c31407795e75e3fed8617a1"
LA = os.environ["LOCALAPPDATA"]
DEPLOY = open(os.path.join(LA, "hermes", "profiles", "cto", ".cf_deploy_token"),
              encoding="utf-8").read().strip()
OUT = os.path.join(LA, "hermes", "profiles", "cto", ".cf_container_d1_token")


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request("https://api.cloudflare.com/client/v4" + path, data=data,
                                 method=method,
                                 headers={"Authorization": f"Bearer {DEPLOY}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "_body": e.read()[:300].decode("utf-8", "ignore")}


name = f"cfb-container-d1 ({time.strftime('%Y-%m-%d')})"
res = call("POST", f"/accounts/{ACCT}/tokens", {
    "name": name,
    "policies": [{
        "effect": "allow",
        "resources": {f"com.cloudflare.api.account.{ACCT}": "*"},
        "permission_groups": [{"id": D1_READ}, {"id": D1_WRITE}],   # read + write: the container archives
    }],
})
if not res.get("success"):
    print("CREATE FAILED:", res.get("_http"), res.get("_body") or res.get("errors"))
    sys.exit(1)
tid, val = res["result"]["id"], res["result"]["value"]

os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w", encoding="utf-8") as f:
    f.write(val + "\n")
try:
    os.chmod(OUT, 0o600)
except Exception:
    pass

fp = hashlib.sha256(val.encode()).hexdigest()[:12]
print(f"created: {name!r}\n  id          = {tid}\n  fingerprint = sha256[:12] {fp}\n"
      f"  value       = NOT PRINTED\n  stored to   = {OUT}")
sys.exit(0)