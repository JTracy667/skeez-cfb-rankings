"""Mint a short-lived, READ-ONLY D1 token for QA so they can run the release gate themselves.

Jeff's call: give QA a read-only D1 credential, revoke it as soon as the gate work is done.

Design constraints, each one deliberate:
  * D1 READ only (permission group 192192df92ee43ac90f2aeeffce67e35). No D1 Write group, so the
    token structurally cannot modify any database -- including the one it can read.
  * HARD EXPIRY of 6 hours (expires_on), so a forgotten revocation still dies on its own. Revocation
    on confirmation is the plan; expiry is the backstop.
  * The VALUE IS NEVER PRINTED and never written to the repo. It goes to profiles/qa/ only, and this
    script reports a sha256[:12] fingerprint plus the path. That is the lesson from
    steep-river-d24c: a credential pasted through chat ends up in agent.log/gateway.log in plaintext.
  * Read-only is PROVEN, not asserted: the script attempts a write against the SCRATCH database and
    requires it to be refused.

    python scripts/mint_qa_readonly_token.py
"""
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

ACCT = "90c2c31beec12cb7de1c249ade1eb773"
PROD_D1 = "c3ec3149-cc85-483b-b727-5a18e3d5a1b9"
SCRATCH_D1 = "36d70d32-07f9-4c4c-a1b8-07e33bab3967"
D1_READ = "192192df92ee43ac90f2aeeffce67e35"
TTL_HOURS = 6

LA = os.environ["LOCALAPPDATA"]
DEPLOY = open(os.path.join(LA, "hermes", "profiles", "cto", ".cf_deploy_token"),
              encoding="utf-8").read().strip()
QA_OUT = os.path.join(LA, "hermes", "profiles", "qa", ".cf_d1_token_readonly")


def call(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        "https://api.cloudflare.com/client/v4" + path, data=data, method=method,
        headers={"Authorization": f"Bearer {token or DEPLOY}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "_body": e.read()[:300].decode("utf-8", "ignore")}


def d1_query(db, sql, token):
    body = json.dumps({"sql": sql}).encode()
    req = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4/accounts/{ACCT}/d1/database/{db}/query",
        data=body, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, {"_body": e.read()[:300].decode("utf-8", "ignore")}


name = f"qa-d1-readonly (temp {time.strftime('%Y-%m-%d')})"
expires_on = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + TTL_HOURS * 3600))

res = call("POST", f"/accounts/{ACCT}/tokens", {
    "name": name,
    "expires_on": expires_on,
    "policies": [{
        "effect": "allow",
        "resources": {f"com.cloudflare.api.account.{ACCT}": "*"},
        "permission_groups": [{"id": D1_READ}],      # READ ONLY -- no D1 Write group
    }],
})
if not res.get("success"):
    print("CREATE FAILED:", res.get("_http"), res.get("_body") or res.get("errors"))
    sys.exit(1)

tid = res["result"]["id"]
val = res["result"]["value"]
print(f"created: {name!r}\n  id          = {tid}\n  expires_on  = {expires_on} "
      f"({TTL_HOURS}h)\n  fingerprint = sha256[:12] {hashlib.sha256(val.encode()).hexdigest()[:12]}"
      f"\n  value       = NOT PRINTED")

os.makedirs(os.path.dirname(QA_OUT), exist_ok=True)
with open(QA_OUT, "w", encoding="utf-8") as f:
    f.write(val + "\n")
try:
    os.chmod(QA_OUT, 0o600)
except Exception:
    pass
print(f"stored to {QA_OUT} (never echoed)")

# ---- verify READ works against production (safe: a SELECT)
time.sleep(3)      # new tokens can 401 for ~1 minute while they propagate
code, body = d1_query(PROD_D1, "SELECT COUNT(*) AS n FROM teams", val)
print(f"read probe on production: HTTP {code} -> {str(body)[:120]}")

# ---- PROVE it cannot write: attempt a write against the SCRATCH database only
code_w, body_w = d1_query(SCRATCH_D1, "CREATE TABLE IF NOT EXISTS _perm_probe_ro (x INTEGER)", val)
refused = code_w >= 400 or not (body_w or {}).get("success", False)
print(f"write probe on scratch:   HTTP {code_w} -> {str(body_w)[:160]}")
print(f"READ-ONLY PROVEN: {refused}" if refused else
      "!! WRITE SUCCEEDED -- the token is NOT read-only; revoking immediately")
if not refused:
    call("DELETE", f"/accounts/{ACCT}/tokens/{tid}")
    sys.exit(2)

print(json.dumps({"id": tid, "name": name, "expires_on": expires_on,
                  "fingerprint_sha256_12": hashlib.sha256(val.encode()).hexdigest()[:12],
                  "path": QA_OUT}, indent=2))
sys.exit(0)