#!/usr/bin/env python3
"""selftest_degraded_mode.py — prove budget breach => stop calling + staleness flag.

D1_CHECKLIST line 55 requires degraded mode be "test proven", not asserted. This
runs the real decision path (budget.should_call -> cfbd_shared.cfbd_get) with a
poisoned ledger and asserts the outbound HTTP call is NEVER made.

Exit 0 = all checks pass. Exit 1 = at least one failed (same contract as
scripts/selftest_zero_writes.py).

Run:  python scripts/selftest_degraded_mode.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import budget          # noqa: E402
import cfbd_shared     # noqa: E402

FAILS: list[str] = []
CHECKS = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        FAILS.append(label)


def poison(source: str, pct: float) -> None:
    """Put `source` at `pct`% of its cap in the in-memory ledger."""
    d = budget.state()
    now = datetime.now(timezone.utc)
    cap = budget.CAPS[source]
    bucket = "day" if cap["period"] == "day" else "month"
    key = now.strftime("%Y-%m-%d") if bucket == "day" else now.strftime("%Y-%m")
    d[bucket].setdefault(key, {})[source] = {"calls": int(cap["limit"] * pct / 100.0)}


def main() -> int:
    # Isolate: temp file mirror, fresh in-memory state. Never touches D1.
    tmp = tempfile.mkdtemp(prefix="budget_selftest_")
    budget.LEDGER_PATH = os.path.join(tmp, "ledger.json")
    budget._state = {"schema": 1, "day": {}, "month": {}, "paused": {}}
    budget.FLUSH_EVERY_S = 10 ** 9          # never flush during the test
    budget._last_flush = 0.0
    budget._paused.clear()

    # ---------------------------------------------------------------- thresholds
    print("\n1. threshold levels (80% alert / 95% pause)")
    for pct, want in ((50.0, "ok"), (79.9, "ok"), (80.0, "alert"), (94.9, "alert"), (95.0, "pause")):
        poison("propline", pct)
        got = budget.status("propline")["level"]
        check(f"propline at {pct}% -> {want}", got == want, f"got {got}")

    # ------------------------------------------------------------- decision gate
    print("\n2. should_call gate")
    poison("propline", 96.0)
    check("96% => should_call False", budget.should_call("propline") is False)
    poison("propline", 85.0)
    check("85% => should_call True (alert, not paused)", budget.should_call("propline") is True)
    poison("propline", 10.0)
    check("10% => should_call True", budget.should_call("propline") is True)

    # ------------------------------------------- the call actually does NOT go out
    print("\n3. a paused source makes NO outbound HTTP call (the real proof)")
    poison("cfbd", 99.0)
    check("cfbd status is pause", budget.status("cfbd")["level"] == "pause")

    calls = {"n": 0}

    class _Tripwire:
        """Any real CFBD request would land here and be recorded."""
        def get(self, *a, **k):
            calls["n"] += 1
            raise AssertionError("cfbd_get made an HTTP call while PAUSED")

    real_client = cfbd_shared._CLIENT
    cfbd_shared._CLIENT = _Tripwire()
    try:
        out = cfbd_shared.cfbd_get("games", year=2026, week=1, retries=3)
    finally:
        cfbd_shared._CLIENT = real_client

    check("cfbd_get returned [] while paused", out == [], f"got {type(out).__name__}/{out!r}")
    check("zero HTTP attempts reached the client", calls["n"] == 0, f"{calls['n']} attempts")

    # ------------------------------------------------------------ staleness flag
    print("\n4. staleness flag surfaces for the serving layer")
    rep = budget.burn_report()
    check("burn_report marks cfbd paused", "cfbd" in rep["paused"], f"paused={rep['paused']}")
    poison("propline", 99.0)
    rep = budget.burn_report()
    check("both paused sources reported", set(rep["paused"]) >= {"cfbd", "propline"},
          f"paused={rep['paused']}")

    # ------------------------------------------------------------------ recovery
    print("\n5. recovery: under the threshold calls resume")
    poison("cfbd", 10.0)
    cfbd_shared._CLIENT = _Tripwire()
    try:
        # Paused gate must let it through to the client; the tripwire then raises,
        # which cfbd_get swallows into [] — so what we assert is that it TRIED.
        cfbd_shared.cfbd_get("games", year=2026, retries=1)
    finally:
        cfbd_shared._CLIENT = real_client
    check("below threshold the call is attempted again", calls["n"] >= 1, f"{calls['n']} attempts")
    check("recovered source is not flagged paused", "cfbd" not in budget.burn_report()["paused"])

    print(f"\n{CHECKS - len(FAILS)}/{CHECKS} PASS")
    if FAILS:
        print("FAILED: " + "; ".join(FAILS))
        return 1
    print("degraded mode: VERIFIED (breach stops calls, flags staleness, recovers)")
    return 0


if __name__ == "__main__":
    sys.exit(main())