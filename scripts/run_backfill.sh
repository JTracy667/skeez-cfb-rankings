#!/usr/bin/env bash
# Launch the D1 backfill DETACHED, so a turn/session timeout can never kill it
# (that is exactly what halted the first run mid-2023 and left a stale lock).
#
# Usage:  bash scripts/run_backfill.sh [--seasons 2023 2024 ...]
# Progress: data/backfill_checkpoint.json (per-chunk confirmed counts) + logs/backfill.log
# Single runner: backfill_d1.py takes data/backfill.lock and steals it if the
# holder pid is dead, so re-running this after a kill resumes safely.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 1
mkdir -p logs

if [ -z "${CF_D1_TOKEN:-}" ]; then
  # Local dev token file (see D1_RISK_REGISTER): never commit or echo this value.
  TOKFILE="${USERPROFILE:-$HOME}/Desktop/Cloudflare.txt"
  CF_D1_TOKEN="$(grep -o 'cfat_[A-Za-z0-9_-]*' "$TOKFILE" 2>/dev/null | head -1)"
  if [ -z "$CF_D1_TOKEN" ]; then
    echo "FATAL: no CF_D1_TOKEN in env and none found in $TOKFILE" >&2
    exit 1
  fi
fi
export CF_D1_TOKEN

echo "=== backfill launch $(date -Is) args='$*' ===" >> logs/backfill.log
nohup python -u scripts/backfill_d1.py "$@" >> logs/backfill.log 2>&1 &
PID=$!
echo "backfill started detached: pid=$PID"
echo "  checkpoint : $REPO/data/backfill_checkpoint.json"
echo "  log        : $REPO/logs/backfill.log"
echo "  status     : python scripts/backfill_d1.py --status"