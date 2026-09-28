"""CHOKEPOINT GUARD — no serving, build or import code may read `data/` off the ephemeral disk.

WHY THIS EXISTS
The v50 incident: the site served `data/cfbd_analytics.json` -- a file baked into the image
on a container whose filesystem is EPHEMERAL -- instead of the D1 copy the weekly pull had
written. A week of pulls never reached a visitor. The trap was ALREADY written down in
`CLOUDFLARE_DEPLOY.md`; a note does not fail a build, so it happened anyway. This is the
control that does.

THE RULE
Code on the serve path, the board-BUILD path, or the import path must not read a file
under `data/`. The container disk is discarded on every recycle (`sleepAfter` 5m), so a
runtime write can be served once and then silently revert. A build that reads a stale disk
file is worse: it bakes the staleness into D1.

MEASURED, NOT GUESSED -- AND IN A FRESH PROCESS
Each scope is measured by `tests/_reads_probe.py` in its OWN interpreter. Measuring
in-process made the answer depend on whether another test module had already imported
`app` and warmed its caches, so the same guard passed alone and failed in a combined run.
A guard whose answer depends on test ordering is not a guard.

An entry marked INTERMITTENT is exempt from the stale-entry check only: its read sits on a
branch gated by an external call, so it appears in some runs and not others. Everything
else -- unknown files, and entries with no justification -- fails hard.

The observed set must be a SUBSET of the scope's allowlist. So it passes today with the
known offenders listed and justified, and ANY NEW disk read fails immediately. Every
allowlist entry must name the finding that owns it (F<n>) or be marked PERMANENT, plus the
plan phase that removes it -- so the list cannot rot into false coverage. When all three
allowlists are empty the rule is absolute.

`CFB_GUARD_DISCOVER=1` prints what was measured (and which route/caller touched it).

Run:  python -m pytest tests/test_no_disk_reads_in_serving.py -v
"""
import json
import os
import pathlib
import re
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
PROBE = REPO / "tests" / "_reads_probe.py"
DISCOVER = os.environ.get("CFB_GUARD_DISCOVER") == "1"

# --------------------------------------------------------------------------- ALLOWLISTS
# Measured 2026-09-28 with CFB_GUARD_DISCOVER=1. Exit criteria for each phase is: delete
# that phase's entries from the list below.

SERVE_ALLOWLIST: dict[str, str] = {
    "cfbd_analytics.json": "F7/Phase 6 - _served_analytics() reads the image file for IDENTITY/string fields + fallback; numerics come from D1. Documented design, but the identity half is still ephemeral-baked.",
    "odds_cache.json":     "F2/Phase 6 - licensed odds cache read while serving; move behind a D1-first accessor",
    "line_movements.json": "F2/Phase 6 - line-movement history read while serving; same treatment",
    "active_injuries.json": "F2/Phase 6 - injury board read while serving; D1 injury_snapshots exists but is not the served source",
    "teams.json":          "F3/Phase 6 - team identity read from disk while D1 `teams` also exists (two sources, no live writer)",
    "budget_ledger.json":  "F2/Phase 6 - /api/health reads the quota ledger from disk although D1 api_usage is the ledger of record",
    # DIFFED OUT in Phase 2 (2026-09-28) once results and the record moved to D1:
    #   finals_cache.json -- the ephemeral disk cache is deleted; _fetch_final_scores
    #                        archives to D1 `games` and falls back to D1.
    #   record.json      -- the SU/ATS record now lives in D1 app_state (su_ats_record).
    "best_bets.json":      "F1/Phase 6 - best-bets board still read from disk. NOT covered by Phase 2: the board itself (not the results source) is what needs a D1 door.",
    # read by `import app` itself, which every scope does
    "cfbd_logos.json": "PERMANENT - static reference asset (baked into the image, never written at runtime); no revert risk",
    # INTERMITTENT: this read is taken only on a branch gated by an external call, so it
    # appears in some runs and not others. git-tracked, last written 2026-08-07 by
    # scripts/build_fbs_db.py -- never written at runtime.
    "fbs_teams.json": "INTERMITTENT PERMANENT - static reference asset written only by a build script, never at runtime",
}

BUILD_ALLOWLIST: dict[str, str] = {
    # These matter MORE than the serve reads: a board built from a stale ephemeral file
    # bakes the staleness into D1.
    "week_schedule.json":   "F2/Phase 3 - load_schedule() reads it, and the schedule board is then persisted",
    "active_injuries.json": "F2/Phase 6 - compute_win_totals() reads it before persisting the board",
    "cfbd_analytics.json":  "F7/Phase 6 - compute_win_totals() reads it; a board built from the image file persists the staleness",
    "teams.json":           "F3/Phase 6 - compute_win_totals() reads team identity from disk while D1 `teams` also exists",
    "budget_ledger.json":   "F2/Phase 6 - compute_win_totals() reads the quota ledger from disk",
    "cfbd_logos.json": "PERMANENT - static reference asset (baked into the image, never written at runtime); no revert risk",
}

IMPORT_ALLOWLIST: dict[str, str] = {
    "cfbd_logos.json": "PERMANENT - static reference asset (baked into the image, never written at runtime); no revert risk",
}

SCOPES = {
    "IMPORT": (IMPORT_ALLOWLIST, "import"),
    "SERVE": (SERVE_ALLOWLIST, "serve"),
    "BUILD": (BUILD_ALLOWLIST, "build"),
}


def _measure(scope_arg: str) -> dict[str, list[str]]:
    env = dict(os.environ)
    for k, v in (("CFB_SKIP_BOOTWARM", "1"), ("REFRESH_INTERVAL_SECONDS", "0"),
                 ("D1_WRITE_ENABLED", "1")):
        env.setdefault(k, v)
    proc = subprocess.run([sys.executable, str(PROBE), scope_arg],
                          capture_output=True, text=True, cwd=str(REPO), env=env, timeout=900)
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
    assert line, (
        f"reads probe ({scope_arg}) produced no JSON (rc={proc.returncode}). The guard cannot "
        f"run, which is a failure, not a pass.\nstdout: {proc.stdout[-1200:]}\n"
        f"stderr: {proc.stderr[-1200:]}")
    return json.loads(line[-1])


@pytest.mark.parametrize("scope", list(SCOPES))
def test_no_unlisted_disk_reads(scope):
    allowlist, arg = SCOPES[scope]
    reads = _measure(arg)

    if DISCOVER:
        print(f"\n=== {scope} ===")
        if not reads:
            print("  (no data/ file read)")
        for f in sorted(reads):
            print(f"  {f}  <- {', '.join(reads[f])}")
        return

    unexpected = sorted(set(reads) - set(allowlist))
    assert not unexpected, (
        f"{scope}: these data/ files were read but are NOT on the allowlist:\n"
        + "\n".join(f"  - {f}  <- {', '.join(reads[f])}" for f in unexpected)
        + "\n\nThe container disk is ephemeral: a runtime write to one of these can be "
          "served once and then silently revert (the v50 defect). Route it through a "
          "D1-first accessor, like `_served_analytics()` -- see "
          "docs/DATA_PERSISTENCE_PLAN.md Phase 6. Do NOT allowlist without a finding id "
          "or an explicit PERMANENT marker, plus the phase that removes it."
    )

    # An INTERMITTENT entry may legitimately be absent from a given run (its branch is
    # gated by an external call); it is still checked when it DOES appear, and it is still
    # a hard failure if it appears without being listed.
    stale = sorted(f for f in set(allowlist) - set(reads)
                   if "INTERMITTENT" not in allowlist[f])
    assert not stale, (
        f"{scope}: allowlisted files that are no longer read -- delete these entries so "
        "the list stays trustworthy:\n"
        + "\n".join(f"  - {f} ({allowlist[f]})" for f in stale)
    )

    # An entry is valid if it names the finding that owns it (F<n>) OR is explicitly
    # PERMANENT -- and, unless PERMANENT, says which phase removes it.
    bad = [f for f, why in allowlist.items()
           if not (re.search(r"\bF\d", why) or "PERMANENT" in why)
           or ("PERMANENT" not in why and "Phase" not in why)]
    assert not bad, (
        f"{scope}: every allowlist entry needs a finding id (F<n>) or PERMANENT, and the "
        "phase that removes it:\n" + "\n".join(f"  - {f}" for f in bad)
    )