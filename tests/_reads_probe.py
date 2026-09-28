"""Measure which `data/` files a given code path opens. Prints one JSON line.

Run as:  python tests/_reads_probe.py {import|serve|build}

Runs in its OWN interpreter on purpose. Measuring in-process made the result depend on
whether another test module had already imported `app` and warmed its caches -- so the
same guard passed alone and failed in a combined run. A guard with a variable answer is
not a guard.

The spy patches BOTH `builtins.open` and `io.open`: a bare `open(...)` resolves via
builtins, while `Path.open()`/`Path.read_text()` call io.open. Patching only one leaves a
blind spot -- and that blind spot hid exactly the `open(_CFBD_ANALYTICS_FILE)` call in
`_load_cfbd_analytics_file()`, i.e. the v50 defect itself.
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
os.environ.setdefault("D1_WRITE_ENABLED", "1")

# Every GET route in app.py (path params filled with plausible values).
ENDPOINTS = [
    "/", "/analytics", "/schedule", "/win-totals", "/ping",
    "/api/health", "/api/rankings", "/api/rankings/1", "/api/rankings/conf/SEC",
    "/api/rankings/search?q=Georgia",
    "/api/analytics", "/api/analytics/pull-status",
    "/api/schedule", "/api/schedule/current-week", "/api/schedule/weeks",
    "/api/win-totals", "/api/best-bets", "/api/best-bets/record", "/api/record",
    "/api/odds", "/api/injuries", "/api/line-movements", "/api/matchup",
    "/api/projections", "/api/budget", "/api/boards/status",
]

reads: dict[str, set[str]] = {}
_label = "?"
_real_io = io.open
_real_builtin = builtins.open


def _spy(file, *a, **k):
    mode = a[0] if a else k.get("mode", "r")
    try:
        p = pathlib.Path(str(file)).resolve()
        if DATA_DIR in p.parents and not str(mode).startswith(("w", "a", "x", "+")):
            reads.setdefault(p.name, set()).add(_label)
    except Exception:  # noqa: BLE001 - the spy must never break the code under test
        pass
    return _real_io(file, *a, **k)


def _note(label):
    global _label
    _label = label


def main() -> int:
    scope = sys.argv[1] if len(sys.argv) > 1 else "serve"
    sys.path.insert(0, str(REPO))
    io.open = _spy
    builtins.open = _spy
    try:
        _note("import app")
        import app  # noqa: PLC0415

        if scope == "import":
            pass
        elif scope == "serve":
            from fastapi.testclient import TestClient  # noqa: PLC0415
            client = TestClient(app.app)
            for ep in ENDPOINTS:
                _note(f"GET {ep}")
                try:
                    client.get(ep)
                except Exception:  # noqa: BLE001 - an endpoint error is not this probe's subject
                    pass
        elif scope == "build":
            for label, call in (
                ("load_schedule()", lambda: app.load_schedule()),
                ("compute_win_totals()", lambda: app.compute_win_totals()),
                ("get_rankings()", lambda: app.get_rankings()),
            ):
                _note(label)
                try:
                    call()
                except Exception:  # noqa: BLE001
                    pass
        else:
            print(f"unknown scope {scope!r}", file=sys.stderr)
            return 2
    finally:
        io.open = _real_io
        builtins.open = _real_builtin

    print(json.dumps({f: sorted(v) for f, v in reads.items()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())