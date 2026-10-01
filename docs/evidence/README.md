# Evidence: data/cfbd_analytics.json damage, 2026-09-30

Preserved because QA remediation §1 requires the incident be documented and forbids an
unrecorded restoration. Nothing here is a runtime input; this directory is an evidence record.

## What happened

| | |
|---|---|
| File | `data/cfbd_analytics.json` |
| Damaged state | 73 bytes, one stub row (`name: "Team"`, `team_id: 1`, `sp_plus: 30`) |
| Damaged SHA-256 | `8fa51948…94d1` (matches QA's independent read-back) |
| mtime | 2026-09-30 16:11:04 PT |
| Size before | ~77,400 lines / 685 teams |
| Git impact | 77,406 deletions, working tree only (never committed) |

The same minute-scale window (16:09–16:11 PT) also rewrote `best_line_store.json`,
`best_line_ts.json`, `budget_ledger.json` and `active_injuries.json` — the signature of a
pull/refresh run, not an editor. QA states a delegated probe invoked
`run_weekly_analytics_pull()` and takes responsibility.

## Preservation

`cfbd_analytics.json.damaged-2026-09-30T1611.json` is a byte copy of the damaged file, taken
BEFORE any restoration. Its SHA-256 is the `8fa51948…94d1` above.

## Restoration

Restored from git HEAD, which is the last committed copy of the image-baked fallback
(685 teams, Georgia `sp_plus` 30.2 — consistent with the D1 publication data). Restoration
was explicitly approved by Jeff in-session; QA's order forbids `git checkout`/`git restore`
on this file without approval, so the approval is recorded in the remediation handoff.

**Approval, verbatim** — assistant: *"I need one decision from you: restore
`data/cfbd_analytics.json` from HEAD, or leave it alone? My recommendation is restore from
HEAD."* → Jeff: *"Do what you need to do."*

## Prevention

The damage was possible because a test reached a real write-through path. Fixed at the root:
every runtime mirror resolves through `runtime_paths.data_dir()`, `tests/conftest.py` points
`CFB_DATA_DIR` at a scratch copy of `data/` before `app` is imported, and
`scripts/run_enforcement_tests.py` re-hashes the repository's `data/*.json` before and after a
run and FAILS the gate if any of them changed. Verified: repository `data/` byte-identical
across a full suite run while the scratch copy received the writes.