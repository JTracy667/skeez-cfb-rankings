"""Prove the revocation propagation window, so a t+0 "still works" reading is not ambiguous.

Context: revoking the QA token returned success=True and the token disappeared from the account
list, but an immediate D1 read with its value returned HTTP 200. Cloudflare token changes are
eventually consistent (minting the QA token showed the same ~1min window in reverse). The QA value
was never retained -- by design -- so it cannot be re-probed; this script demonstrates the
mechanism instead, on a throwaway READ-ONLY token, and polls until the value is provably dead.

Read-only scope, no writes attempted, and it revokes what it mints even on failure.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

ACCT = "90c2c31beec12cb7de1c249ade1eb773"
PROD_D1 = "c3ec3149-cc85-483b-b727-5a18e3d5a1b9"
D1_READ = "192192df92ee43ac90f2aeeffce67e35"
LA = os.environ["LOCALAPPDATA"]
DEPLOY = open(os.path.join(LA, "hermes", "profiles", "cto", ".cf_deploy_token"),
              encoding="utf-8").read().strip()


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request("https://api.cloudflare.com/client/v4" + path, data=data,
                                 method=method,
                                 headers={"Authorization": f"Bearer {DEPLOY}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "_body": e.read()[:200].decode("utf-8", "ignore")}


def probe(token):
    """A read of production D1 with the given value. Returns the HTTP status."""
    body = json.dumps({"sql": "SELECT 1 AS ok"}).encode()
    req = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4/accounts/{ACCT}/d1/database/{PROD_D1}/query",
        data=body, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


created = api("POST", f"/accounts/{ACCT}/tokens", {
    "name": f"revocation-proof (temp {time.strftime('%Y-%m-%d %H:%M')})",
    "expires_on": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600)),
    "policies": [{"effect": "allow",
                  "resources": {f"com.cloudflare.api.account.{ACCT}": "*"},
                  "permission_groups": [{"id": D1_READ}]}],
})
if not created.get("success"):
    print("mint failed:", created.get("_http"), created.get("_body"))
    sys.exit(1)
tid, val = created["result"]["id"], created["result"]["value"]
print(f"minted throwaway read-only token id={tid} (value never printed)")

# Let it become valid, then confirm it reads.
t0 = time.time()
status = None
while time.time() - t0 < 180:
    status = probe(val)
    if status == 200:
        print(f"t+{int(time.time() - t0):>3}s  read probe -> HTTP {status} (token live)")
        break
    time.sleep(10)
else:
    print(f"token never became live (last HTTP {status}); revoking and stopping")
    api("DELETE", f"/accounts/{ACCT}/tokens/{tid}")
    sys.exit(1)

r = api("DELETE", f"/accounts/{ACCT}/tokens/{tid}")
print(f"revoked: success={r.get('success')} {r.get('_http', '')}")

t1 = time.time()
while time.time() - t1 < 240:
    s = probe(val)
    print(f"t+{int(time.time() - t1):>3}s after revoke  read probe -> HTTP {s}")
    if s != 200:
        print(f"PROVEN DEAD after {int(time.time() - t1)}s (HTTP {s})")
        break
    time.sleep(15)
else:
    print("!! still working after 240s -- revocation did not take effect, investigate")

left = [t["name"] for t in (api("GET", f"/accounts/{ACCT}/tokens").get("result") or [])]
print("remaining account tokens:", left)