"""Revoke the temporary QA read-only D1 token, and remove its file.

One command, so revocation does not depend on remembering an id:

    python scripts/revoke_qa_readonly_token.py

Finds the token BY NAME (so a re-minted one is caught too), deletes it, then deletes the credential
file QA was given. Verifies both afterwards. Safe to run when the token is already gone.
"""
import json
import os
import sys
import urllib.error
import urllib.request

ACCT = "90c2c31beec12cb7de1c249ade1eb773"
NAME_PREFIX = "qa-d1-readonly"
LA = os.environ["LOCALAPPDATA"]
QA_FILE = os.path.join(LA, "hermes", "profiles", "qa", ".cf_d1_token_readonly")
DEPLOY = open(os.path.join(LA, "hermes", "profiles", "cto", ".cf_deploy_token"),
              encoding="utf-8").read().strip()


def call(method, path):
    req = urllib.request.Request(
        "https://api.cloudflare.com/client/v4" + path, method=method,
        headers={"Authorization": f"Bearer {DEPLOY}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "_body": e.read()[:200].decode("utf-8", "ignore")}


found = [t for t in (call("GET", f"/accounts/{ACCT}/tokens").get("result") or [])
         if t.get("name", "").startswith(NAME_PREFIX)]
if not found:
    print(f"no token named {NAME_PREFIX!r} -- nothing to revoke")
for t in found:
    r = call("DELETE", f"/accounts/{ACCT}/tokens/{t['id']}")
    print(f"revoked {t['name']!r} id={t['id']}: success={r.get('success')} {r.get('_http', '')}")

if os.path.exists(QA_FILE):
    with open(QA_FILE, encoding="utf-8") as f:
        stale = f.read().strip()
    os.remove(QA_FILE)
    print(f"removed the credential file {QA_FILE}")
    # Prove the value is dead, not just deleted from disk. POLL: revocation is eventually
    # consistent -- a t+0 probe can still return 200 (measured: 200 at t+0, 401 at t+15s), and a
    # single read would report that window as "still works".
    t0 = time.time()
    while time.time() - t0 < 180:
        body = json.dumps({"sql": "SELECT 1"}).encode()
        req = urllib.request.Request(
            f"https://api.cloudflare.com/client/v4/accounts/{ACCT}/d1/database/"
            "c3ec3149-cc85-483b-b727-5a18e3d5a1b9/query", data=body,
            headers={"Authorization": f"Bearer {stale}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                print(f"  t+{int(time.time() - t0):>3}s  HTTP {r.status} (propagation window)")
        except urllib.error.HTTPError as e:
            print(f"  t+{int(time.time() - t0):>3}s  HTTP {e.code} -- PROVEN DEAD")
            break
        time.sleep(10)
    else:
        print("  !! still working after 180s -- investigate")
else:
    print(f"no credential file at {QA_FILE}")

left = [t["name"] for t in (call("GET", f"/accounts/{ACCT}/tokens").get("result") or [])]
print("remaining account tokens:", left)
sys.exit(0)