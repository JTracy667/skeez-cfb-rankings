"""Probe: which `data/` files does `import app` open?  Run as a SUBPROCESS.

Import-time reads are the v50 class: `data/cfbd_analytics.json` was a file baked into
the image and read off the container's EPHEMERAL disk. A fresh interpreter is the only
way to measure import-time reads honestly (an in-process test would find `app` already
in `sys.modules` and miss them).

Prints one JSON line: a sorted list of file names read from `data/` during import.
Called by tests/test_no_disk_reads_in_serving.py.
"""
import builtins
import io
import json
import os
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
DATA_DIR = (REPO / "data").resolve()

os.environ.setdefault("CFB_SKIP_BOOTWARM", "1")
os.environ.setdefault("REFRESH_INTERVAL_SECONDS", "0")

found: set[str] = set()
_real_io_open = io.open
_real_builtin_open = builtins.open  # bare `open(...)` resolves here, NOT to io.open


def _spy(file, *a, **k):
    mode = a[0] if a else k.get("mode", "r")
    try:
        p = pathlib.Path(str(file)).resolve()
        if DATA_DIR in p.parents and not str(mode).startswith(("w", "a", "x", "+")):
            found.add(p.name)
    except Exception:  # noqa: BLE001
        pass
    return _real_io_open(file, *a, **k)


io.open = _spy
builtins.open = _spy
sys.path.insert(0, str(REPO))
try:
    import app  # noqa: F401
finally:
    io.open = _real_io_open
    builtins.open = _real_builtin_open

print(json.dumps(sorted(found)))