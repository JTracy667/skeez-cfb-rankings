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
    # DIFFED OUT in Phase 6 (2026-09-28): identity/string fields (conf, streak) now live in D1
    # (app_state key `analytics_identity`, written by _store_analytics_identity on every pull),
    # so _served_analytics() reads the disk file only when D1 has no identity map at all.

    # DIFFED OUT in Phase 6 (2026-09-28): the odds cache is D1-FIRST now (_odds_cache_payload,
    # app_state key `odds_cache`, kill switch ODDS_CACHE_FROM_D1). This one also buys back real
    # quota: the cache exists to avoid re-fetching metered odds after a recycle, which an
    # ephemeral file could never do.
    # DIFFED OUT in Phase 6 (2026-09-28): the rolling 7-day CLV record is D1-FIRST now
    # (_movement_payload, app_state key `line_movements`, kill switch MOVEMENTS_FROM_D1). This
    # was not a mere cache -- it is the audit trail of what the line did, so a recycle used to
    # destroy the record itself.
    # DIFFED OUT in Phase 6 (2026-09-28): _load_injuries_doc() is D1-FIRST now (app_state
    # `active_injuries`, kill switch INJURIES_FROM_D1). Note D1 `injury_snapshots` is a
    # SETTLED-OUTCOME tracking table, not a home for the current injury state -- the door
    # uses app_state.
    "teams.json":          "PERMANENT - static reference asset (baked into the image, never written at runtime); read by load_local(). No revert risk.",
    # DIFFED OUT in Phase 6 (2026-09-28): budget.state() is D1-FIRST now (budget.py,
    # BUDGET_FROM_D1 kill switch), so /api/health reads D1 api_usage -- the ledger of record --
    # and the file mirror is only a local-dev fallback.
    # DIFFED OUT in Phase 2 (2026-09-28) once results and the record moved to D1:
    #   finals_cache.json -- the ephemeral disk cache is deleted; _fetch_final_scores
    #                        archives to D1 `games` and falls back to D1.
    #   record.json      -- the SU/ATS record now lives in D1 app_state (su_ats_record).
    # DIFFED OUT in Phase 6 (2026-09-28): the tracked best-bets record is D1-FIRST now
    # (_load_best_bets, app_state key `best_bets`, kill switch BEST_BETS_FROM_D1). Both
    # _lock_best_bets and _ingest_best_bets read-modify-write it, so an ephemeral disk was
    # resetting locked picks and their graded results on every recycle.
    # read by `import app` itself, which every scope does
    "cfbd_logos.json": "PERMANENT - static reference asset (baked into the image, never written at runtime); no revert risk",
    # INTERMITTENT: this read is taken only on a branch gated by an external call, so it
    # appears in some runs and not others. git-tracked, last written 2026-08-07 by
    # scripts/build_fbs_db.py -- never written at runtime.
    "fbs_teams.json": "INTERMITTENT PERMANENT - static reference asset written only by a build script, never at runtime",
    # FINDING F9 (Phase 6 removal). This read happens ONLY when the store
    # holds no verified publication, i.e. the documented disk fallback that keeps a page rendering
    # during an outage instead of serving an empty board. NOT permanent: Phase 6 routes the
    # remaining board reads through the D1-first accessor (_served_analytics()); delete this entry
    # with that conversion, and let this test go red if the read comes back.
    "cfbd_analytics.json": "F9 -> Phase 6 (D1-first _served_analytics); remove with that conversion",
}

BUILD_ALLOWLIST: dict[str, str] = {
    # These matter MORE than the serve reads: a board built from a stale ephemeral file
    # bakes the staleness into D1.
    # DIFFED OUT in Phase 3 (2026-09-28): load_schedule() is D1-first now
    # (app_state `week_schedule`), so the file is no longer read on any measured path.
    # DIFFED OUT in Phase 6: same conversion as the serve scope above.
    # DIFFED OUT in Phase 6 (2026-09-28): get_rankings() and /api/projections now go through
    # _served_analytics() (D1-first). A board can no longer be built from the image file.

    "teams.json":           "PERMANENT - static reference asset (baked into the image, never written at runtime). No revert risk.",
    # DIFFED OUT in Phase 6: same conversion as the serve scope above.
    "cfbd_logos.json": "PERMANENT - static reference asset (baked into the image, never written at runtime); no revert risk",
    # FINDING F9 (Phase 6 removal). Both reads occur ONLY when the store
    # holds no verified publication: cfbd_analytics.json is read by compute_win_totals() to build
    # the win-total board, week_schedule.json by load_schedule() on its serve fallback. NOT
    # permanent: Phase 6 converts both to the D1-first accessors; delete these entries with that
    # conversion, and let this test go red if either read comes back.
    "cfbd_analytics.json": "F9 -> Phase 6 (D1-first compute_win_totals)",
    "week_schedule.json": "F9 -> Phase 6 (D1-first load_schedule)",
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
@pytest.mark.needs_d1
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