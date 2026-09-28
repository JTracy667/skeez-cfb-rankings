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

os.environ.setdefault("CFB_SKIP_BOOTWARM", "1")

# `_fetch_final_scores` live-fetches CFBD /games on a cache miss. Same standing rule as the
# bootwarm guard above: a test run must never spend the metered cap the site serves from.
os.environ.setdefault("CFB_SKIP_LIVE_FETCH", "1")

# Reads default ON and are NOT gated by the write flag (F6). Tests must be able to verify
# that serving reads D1 WITHOUT enabling writes to production D1.
os.environ.setdefault("D1_READ_ENABLED", "1")
