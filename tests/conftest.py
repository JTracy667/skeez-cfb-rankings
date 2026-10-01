"""Test-suite bootstrap: keep pytest off the metered providers.

app.py starts a `bootwarm` daemon thread AT IMPORT that performs live CFBD and
PropLine network calls. It exists for a good production reason -- containers recycle
after 5m idle, so the first visitor after a recycle would otherwise pay a ~12s
cold-cache cost -- but it means `import app` is a live API call. Without this guard
the suite burned real quota and took ~106s instead of ~10s.

Standing rule this enforces: tests must never hit a live metered fetcher, because a
test boot spends the same daily cap the site itself serves from.

Set BEFORE any test module imports app (pytest loads conftest.py first).
"""
import os

import pytest

os.environ.setdefault("CFB_SKIP_BOOTWARM", "1")

# `_fetch_final_scores` live-fetches CFBD /games on a cache miss. Same standing rule as the
# bootwarm guard above: a test run must never spend the metered cap the site serves from.
os.environ.setdefault("CFB_SKIP_LIVE_FETCH", "1")

# Reads default ON and are NOT gated by the write flag (F6). Tests must be able to verify
# that serving reads D1 WITHOUT enabling writes to production D1.
os.environ.setdefault("D1_READ_ENABLED", "1")

# QA remediation §1.5 -- a test run must never write the repository's data/ mirrors.
# A test reached a real write-through path and overwrote data/cfbd_analytics.json with a
# one-row stub (77,406 lines destroyed). Every runtime mirror now resolves through
# runtime_paths.data_dir(), so pointing CFB_DATA_DIR at a scratch COPY of data/ isolates the
# suite: reads still see the real fixtures, writes land in the copy. Set BEFORE app is
# imported. scripts/run_enforcement_tests.py re-hashes the real data/ afterwards to prove it.
if not os.environ.get("CFB_DATA_DIR"):
    import shutil
    import tempfile

    _repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _scratch = tempfile.mkdtemp(prefix="cfb-test-data-")
    shutil.copytree(os.path.join(_repo_root, "data"), _scratch, dirs_exist_ok=True)
    os.environ["CFB_DATA_DIR"] = _scratch

# ── Standing rule: NO TEST MAY WRITE TO PRODUCTION D1 ────────────────────────────────────
# Reads are fine -- the served==D1 parity test needs them. Writes are not: a test run that
# happens to have a token in its environment must not be able to touch the live store.
# Enforced rather than documented, because "it skips when CF_D1_TOKEN is absent" is not a
# guard: a token can appear in a run's environment (tests/d1_counter_selftest.py harvests one
# from Desktop/Cloudflare.txt), and CF_D1_DB_ID DEFAULTS TO PRODUCTION.
#
#   Found 2026-09-30: a full-suite run wrote 2,243 rows into live D1 -- probe rows in
#   stat_observations deleted by exact stamp, so the tables looked unchanged while the write
#   ledger and the Cloudflare burn were charged.
PROD_D1_DB_ID = "c3ec3149-cc85-483b-b727-5a18e3d5a1b9"

# The shared ledger (hermes/d1_write_ledger.json) is PRODUCTION's rows-written counter and the
# daily runaway guard. A test run must not move it: tests fake transports, tokens and database
# ids, and charging those made the counter move (+997, +1) while nothing was written anywhere --
# which is what turned a simple attribution into a hunt. Deterministic, not path-dependent.
os.environ["D1_LEDGER_DISABLED"] = "1"

if os.environ.get("CFB_ALLOW_PROD_TEST_WRITES") != "1":
    import d1_store as _d1_store

    _orig_query_full = _d1_store.query_full

    def _no_prod_writes(sql, params=None, timeout=60):
        stmt = sql.strip().upper()
        if (stmt.startswith(("INSERT", "UPDATE", "DELETE", "REPLACE"))
                and str(getattr(_d1_store, "D1_DB_ID", "")) == PROD_D1_DB_ID):
            raise RuntimeError(
                "refusing to WRITE to production D1 from a test run. Point CF_D1_DB_ID at a "
                "scratch database (scripts/setup_d1_scratch.py), or set "
                "CFB_ALLOW_PROD_TEST_WRITES=1 for a deliberate live probe. SQL: " + sql[:120])
        return _orig_query_full(sql, params, timeout)

    _d1_store.query_full = _no_prod_writes


# ---- D1-dependent tests: SKIP when no usable (non-production) D1 target is configured --------
# A test that needs an external service must skip when that service is not configured; failing
# the suite for an absent credential trains people to ignore red. These 14 tests passed only
# because the repo `.env` happened to supply a token, so every suite run was reading PRODUCTION
# D1 -- and its writes were being refused by the guard above, which is why "a verified result
# should be cached" failed the moment the credential was removed. Point CF_D1_DB_ID at the
# scratch database (scripts/setup_d1_scratch.py) to actually run them.
_OFFLINE_SENTINEL = "local-offline-no-store"


def _has_usable_d1_target() -> bool:
    tok = os.environ.get("CF_D1_TOKEN") or os.environ.get("CLOUDFLARE_API_TOKEN")
    if not tok or tok == _OFFLINE_SENTINEL:
        return False
    return os.environ.get("CF_D1_DB_ID", PROD_D1_DB_ID) not in ("", PROD_D1_DB_ID)


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "needs_d1: requires a real D1 target that is NOT production")


def pytest_collection_modifyitems(config, items):
    if _has_usable_d1_target():
        return
    skip = pytest.mark.skip(
        reason="needs a SCRATCH D1 target (CF_D1_TOKEN + CF_D1_DB_ID away from production); "
               "skipped rather than failed")
    for item in items:
        if "needs_d1" in item.keywords:
            item.add_marker(skip)
