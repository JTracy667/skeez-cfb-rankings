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
