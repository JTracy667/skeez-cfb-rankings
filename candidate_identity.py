"""Identity of the candidate being served — a digest anyone can recompute.

WHY: a claimed commit SHA is not verifiable from the outside. The server can be told any commit
via an env var, and `build` (the Worker's image tag) has already been shown to lie — a warm
instance answers with the new tag while running old code. What a reviewer CAN verify is the bytes
that are actually being served: this module hashes them, the app reports the hash in
`/api/health`, and the reviewer recomputes it from the checkout.

    python candidate_identity.py            # prints "<commit> <digest>"
    python -c "import candidate_identity; print(candidate_identity.digest())"

The set is deliberate and stable: the Python modules that decide behaviour plus the static
assets the pages load. Adding a file to the set changes the digest everywhere at once, so the
server and the reviewer can never disagree about what "the source" is.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))

# Behaviour-defining modules + everything the pages load. Sorted, path-tagged, so a rename or a
# byte change both move the digest.
INCLUDE_SUFFIXES = (".py", ".js", ".html", ".css", ".mjs")
EXCLUDE_DIRS = (".git", "__pycache__", "node_modules", "data", "logs", "docs", "tests", ".venv",
                "scripts")  # scripts/ is tooling -- deploy, verify, serve -- never served by the
                            # app, so a change there must not move the served-source identity


def _files() -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in filenames:
            if fn.endswith(INCLUDE_SUFFIXES):
                out.append(os.path.relpath(os.path.join(dirpath, fn), ROOT).replace("\\", "/"))
    return sorted(out)


def digest() -> str:
    h = hashlib.sha256()
    for rel in _files():
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        try:
            with open(os.path.join(ROOT, rel), "rb") as f:
                h.update(f.read())
        except OSError:
            h.update(b"<unreadable>")
        h.update(b"\0")
    return h.hexdigest()


def commit() -> str:
    """The checkout's HEAD, for context only -- the digest is the thing that proves identity."""
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                              text=True, timeout=10).stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def describe() -> dict:
    return {"commit": commit(), "source_digest": digest(), "files": len(_files())}


if __name__ == "__main__":
    d = describe()
    print(f"{d['commit']} {d['source_digest']} ({d['files']} files)")
    sys.exit(0)