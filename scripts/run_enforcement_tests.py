#!/usr/bin/env python
"""Deploy gate: run the data-layer ENFORCEMENT tests. Non-zero exit = do not ship.

Two suites, both required (see docs/DATA_PERSISTENCE_PLAN.md Phase 1):

  tests/test_served_equals_d1.py           served must equal D1; a differential test
                                           forces them apart to prove D1 is really the
                                           source (the v50 defect as an assertion).
  tests/test_no_disk_reads_in_serving.py   no NEW `data/` read may appear on the serve,
                                           build or import path (allowlist-subset).

WHY THIS IS A SCRIPT AND NOT JUST `pytest`
`pytest.skip` is the polite thing to do when D1 credentials are absent -- and it would make
this gate silently vacuous. So the gate REFUSES to run without `CF_D1_TOKEN` rather than
skipping: a parity check that cannot reach the store must never be read as a pass.

Usage:  python scripts/run_enforcement_tests.py [--all]
        --all   also run the rest of the suite afterwards
"""
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ENFORCEMENT = ["tests/test_served_equals_d1.py", "tests/test_no_disk_reads_in_serving.py"]


def main() -> int:
    if not os.environ.get("CF_D1_TOKEN"):
        print("REFUSING TO RUN: CF_D1_TOKEN is not set.", file=sys.stderr)
        print("The enforcement gate verifies served == D1. Without store credentials it "
              "cannot verify anything, and a skipped check is NOT a pass.", file=sys.stderr)
        return 2

    args = [sys.executable, "-m", "pytest", *ENFORCEMENT, "-q", "-rxs"]
    if "--all" in sys.argv:
        args = [sys.executable, "-m", "pytest", "-q", "-rxs"]
    print("$ " + " ".join(args), flush=True)
    rc = subprocess.call(args, cwd=str(REPO))
    print("\n" + ("ENFORCEMENT GATE: PASS" if rc == 0 else "ENFORCEMENT GATE: FAIL"), flush=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())