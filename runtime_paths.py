"""Where the runtime mirrors live.

Every JSON mirror the app rewrites (analytics, lines, injuries, ledgers, ...) resolves
through here so a single environment variable can redirect all of them.

Why this exists (QA remediation §1.5): a test run reached a real write-through path and
overwrote the repository's `data/cfbd_analytics.json` with a one-row stub, destroying the
77k-line file. A test suite must never write the repository's data/. The suite now points
`CFB_DATA_DIR` at a scratch copy of data/ before app is imported, and the enforcement gate
re-hashes the real data/ afterwards to prove nothing was touched.

Production leaves CFB_DATA_DIR unset.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def data_dir() -> Path:
    override = os.environ.get("CFB_DATA_DIR")
    return Path(override) if override else (ROOT / "data")


def data_file(name: str) -> Path:
    return data_dir() / name