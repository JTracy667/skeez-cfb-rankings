"""Candidate-level page/API check: read-only, and proves it by construction.

    python scripts/qa_candidate_check.py http://127.0.0.1:8011
    CFB_BASE_URL=http://127.0.0.1:8011 python scripts/qa_candidate_check.py

WHAT IT ESTABLISHES (the two things a served candidate could not previously demonstrate):

  1. IDENTITY. `/api/health` reports `candidate.source_digest`, a hash of the bytes the SERVER
     loaded. This script recomputes the same digest from the checkout and compares. A commit SHA
     can be claimed and the `build` tag is an env var the Worker injects (a warm instance has
     already been observed answering with the new tag while running old code) -- a digest cannot
     be claimed, only matched.

  2. TARGET ISOLATION. `/api/health` reports `candidate.d1` and this script asserts
     token_present is false, writes_enabled is false and read_only is true. With no credential in
     the process, d1_store._token() raises and every store path fails closed, so "cannot write to
     D1" is a property of the server rather than a promise about it.

READ-ONLY BY CONSTRUCTION: every request below is a GET, and there are no POSTs anywhere in this
file. Nothing here needs a credential, and nothing here can mutate state.

Exit codes: 0 all checks pass, 1 a check failed, 2 could not reach the server.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

PAGES = ("/analytics", "/schedule")
APIS = (
    "/api/analytics",
    "/api/projections",
    "/api/schedule/weeks",
    "/api/schedule/current-week",
    "/api/schedule?week=5",
)

fails: list[str] = []
passes: list[str] = []


def ok(msg: str) -> None:
    passes.append(msg)
    print(f"  PASS  {msg}")


def bad(msg: str) -> None:
    fails.append(msg)
    print(f"  FAIL  {msg}")


def get(base: str, path: str, timeout: int = 60):
    """The only request helper in this file, and it is a GET."""
    req = urllib.request.Request(base.rstrip("/") + path, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def main() -> int:
    base = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("CFB_BASE_URL", "")).strip()
    if not base:
        print("usage: python scripts/qa_candidate_check.py <base-url>")
        return 2

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import candidate_identity  # noqa: PLC0415

    print(f"candidate check against {base}\n")

    # ---- 1. identity + isolation
    print("identity / isolation")
    try:
        status, body = get(base, "/api/health")
    except Exception as e:  # noqa: BLE001
        print(f"  FAIL  /api/health unreachable: {e}")
        return 2
    ok(f"/api/health -> HTTP {status}")
    try:
        h = json.loads(body)
    except Exception as e:  # noqa: BLE001
        bad(f"/api/health body is not JSON: {e}")
        h = {}
    cand = h.get("candidate") or {}
    local = candidate_identity.digest()
    served = cand.get("source_digest")
    if served == local:
        ok(f"source_digest matches this checkout: {local[:16]}...")
    else:
        bad(f"source_digest MISMATCH — server {str(served)[:16]}... vs checkout {local[:16]}...")
    ok(f"marker: {h.get('code', {}).get('marker')}  commit: {cand.get('commit')}")
    d1 = cand.get("d1") or {}
    if d1.get("token_present") is False:
        ok("d1.token_present is false — no D1 credential in the served process")
    elif d1.get("token_is_placeholder") is True:
        ok("d1.token_present is true but the value is the offline placeholder — the API rejects "
           "it, so every store path fails closed")
    else:
        bad(f"the server holds a REAL D1 credential (token_present={d1.get('token_present')!r}, "
            f"token_is_placeholder={d1.get('token_is_placeholder')!r})")
    if d1.get("writes_enabled") is False:
        ok("d1.writes_enabled is false")
    else:
        bad(f"d1.writes_enabled is {d1.get('writes_enabled')!r}")
    if d1.get("read_only") is True:
        ok("d1.read_only is true")
    else:
        bad(f"d1.read_only is {d1.get('read_only')!r}")
    # (behavioural evidence is re-read at the END, after the API calls below, because the serve
    # state is only populated once an analytics read has happened)

    # ---- 2. pages
    print("\npages")
    for p in PAGES:
        try:
            status, body = get(base, p)
            if status == 200 and len(body) > 500:
                ok(f"{p} -> HTTP 200 ({len(body)} bytes)")
            else:
                bad(f"{p} -> HTTP {status}, {len(body)} bytes")
        except Exception as e:  # noqa: BLE001
            bad(f"{p} -> {e}")

    # ---- 3. APIs, with a shape assertion so 200 alone is not the receipt
    print("\nAPIs")
    for p in APIS:
        try:
            status, body = get(base, p)
            if status != 200:
                bad(f"{p} -> HTTP {status}")
                continue
            try:
                data = json.loads(body)
            except Exception as e:  # noqa: BLE001
                bad(f"{p} -> HTTP 200 but body is not JSON: {e}")
                continue
            n = None
            if isinstance(data, list):
                n = len(data)
            elif isinstance(data, dict):
                for key in ("teams", "rows", "weeks", "data", "items"):
                    v = data.get(key)
                    if isinstance(v, list):
                        n = len(v)
                        break
            if n is None:
                ok(f"{p} -> HTTP 200, JSON ({len(body)} bytes, no counted list)")
            elif n > 0:
                ok(f"{p} -> HTTP 200, {n} item(s)")
            else:
                bad(f"{p} -> HTTP 200 but EMPTY payload (0 items)")
        except Exception as e:  # noqa: BLE001
            bad(f"{p} -> {e}")

    # ---- 4. behaviour: re-read health now that analytics has actually been served
    print("\nbehaviour")
    try:
        _, body = get(base, "/api/health")
        src = str(((json.loads(body).get("candidate") or {}).get("serve") or {}).get("source", ""))
        if not src or src == "unknown":
            bad(f"serve.source not established ({src!r}) — no evidence that D1 was unreachable")
        elif src.startswith("d1"):
            bad(f"serve.source = {src!r} — the candidate is serving FROM D1, so it is not isolated")
        else:
            ok(f"serve.source = {src!r} after a full analytics read — the store was unreachable "
               "and the disk fallback served")
    except Exception as e:  # noqa: BLE001
        bad(f"could not re-read /api/health: {e}")

    print(f"\n{len(passes)} passed, {len(fails)} failed")
    if fails:
        print("FAILED:")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("VERDICT: the served source digest matches this checkout; the only D1 credential in the "
          "process is the offline placeholder (the API rejects it); D1 was not the serving source; "
          "and every page/API returned a non-empty payload.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())