#!/usr/bin/env python
"""Deploy gate: run the data-layer ENFORCEMENT tests. Non-zero exit = do not ship.

Two suites, both required (see docs/DATA_PERSISTENCE_PLAN.md Phase 1):

  tests/test_served_equals_d1.py           served must equal D1; a differential test
                                           forces them apart to prove D1 is really the
                                           source (the v50 defect as an assertion).
  tests/test_no_disk_reads_in_serving.py   no NEW `data/` read may appear on the serve,
                                           build or import path (allowlist-subset).

THIRD SUITE — the page scripts (node --test, no jsdom)
  tests/js/*.test.mjs                      runs analytics.html / schedule.html's OWN
                                           <script> in a node:vm sandbox over a DOM+fetch
                                           stub. Parsing is not evidence: a page parses
                                           cleanly and still dies mid-render on a
                                           ReferenceError, so these assert request order,
                                           deferred projections, debounced search and
                                           rendered row counts.
  scripts/verify_pages_live.mjs            the same harness against the LIVE API.

WHY THIS IS A SCRIPT AND NOT JUST `pytest`
`pytest.skip` is the polite thing to do when D1 credentials are absent -- and it would make
this gate silently vacuous. So the served==D1 parity check is never SKIPPED into a pass: with
no `CF_D1_TOKEN` the gate reports it NOT VERIFIED and exits 3 (PARTIAL), distinct from PASS (0)
and FAIL (1). The credential-free steps still run, so a reviewer without store access gets a
real receipt for everything that does not need the store instead of no receipt at all.

Usage:  python scripts/run_enforcement_tests.py [--all]
        --all   also run the rest of the suite afterwards
"""
import glob
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
D1_PARITY = ["tests/test_served_equals_d1.py"]           # needs CF_D1_TOKEN
HERMETIC = ["tests/test_no_disk_reads_in_serving.py"]    # needs no credentials
EXIT_PARTIAL = 3                                          # ran, but a step is NOT VERIFIED


def _data_hashes() -> dict:
    """QA §1.5: a test run must leave the repository's data/ byte-identical.

    The suite points CFB_DATA_DIR at a scratch copy (tests/conftest.py), so writes go
    elsewhere -- but that is a mechanism, and this is the proof. A run that changes these
    hashes has broken hermeticity, which is how a one-row stub once destroyed
    data/cfbd_analytics.json.
    """
    out = {}
    for p in sorted((REPO / "data").glob("*.json")):
        try:
            out[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError as e:  # noqa: PERF203 - a missing file is itself worth reporting
            out[p.name] = f"unreadable: {e}"
    return out


def main() -> int:
    have_store = bool(os.environ.get("CF_D1_TOKEN"))
    if not have_store:
        print("PARTIAL RUN: CF_D1_TOKEN is not set, so the served==D1 parity check cannot "
              "run.\nIt will be reported NOT VERIFIED -- never as a pass. Every "
              "credential-free step\nstill runs and is reported on its own.", file=sys.stderr)

    data_before = _data_hashes()

    node = shutil.which("node")
    if not node:
        print("REFUSING TO RUN: node is not on PATH; the page-script gate cannot run.",
              file=sys.stderr)
        return 2
    js_files = sorted(glob.glob(str(REPO / "tests" / "js" / "*.test.mjs")))
    if not js_files:
        print("REFUSING TO RUN: no tests/js/*.test.mjs found -- the page-script gate would "
              "silently verify nothing.", file=sys.stderr)
        return 2

    rc = subprocess.call([sys.executable, "-m", "pytest", *HERMETIC, "-q", "-rxs"],
                         cwd=str(REPO))
    if rc == 0 and have_store:
        rc = subprocess.call([sys.executable, "-m", "pytest", *D1_PARITY, "-q", "-rxs"],
                             cwd=str(REPO))
    if rc == 0:
        print("$ node --test tests/js/*.test.mjs", flush=True)
        rc = subprocess.call([node, "--test", *js_files], cwd=str(REPO))
    if rc == 0 and "--all" in sys.argv:
        # scoped to tests/: a bare `pytest` from the root also collects scripts/, where
        # scripts/test_fbs_line_scope.py queries D1 at import and raises SystemExit.
        rc = subprocess.call([sys.executable, "-m", "pytest", "-q", "-rxs", "tests/"],
                             cwd=str(REPO))

    # QA §1.5: prove hermeticity rather than assume it.
    data_after = _data_hashes()
    if data_after != data_before:
        changed = sorted(k for k in set(data_before) | set(data_after)
                         if data_before.get(k) != data_after.get(k))
        print("\nHERMETICITY FAILURE: the test run modified repository data/ files:\n  "
              + "\n  ".join(changed), file=sys.stderr)
        if rc == 0:
            rc = 1

    if rc != 0:
        verdict = "ENFORCEMENT GATE: FAIL"
    elif not have_store:
        verdict = ("ENFORCEMENT GATE: PARTIAL -- served==D1 NOT VERIFIED "
                   "(CF_D1_TOKEN unset); every other step passed")
        rc = EXIT_PARTIAL
    else:
        verdict = "ENFORCEMENT GATE: PASS"
    print("\n" + verdict, flush=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())