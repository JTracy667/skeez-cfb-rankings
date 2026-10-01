#!/usr/bin/env bash
# Launch the D1 backfill DETACHED, so a turn/session timeout can never kill it
# (that is exactly what halted the first run mid-2023 and left a stale lock).
#
# Entry point: starts the SUPERVISOR, which owns the worker.
#   * no worker alive -> supervisor launches one (detached from any turn),
#   * worker stops on the D1 write guard (D1_DAILY_WRITE_CAP, now 2M/day on the
#     Workers PAID plan: 50M rows written/MONTH) -> supervisor waits it out if it
#     ever trips (ledger rolls at UTC midnight = 17:00 PT under PDT) and resumes,
#   * a FAILED chunk (a write D1 did not confirm) -> supervisor exits 1 for a human,
#   * all chunks done -> supervisor exits 0.
#
# Usage:  bash scripts/run_backfill.sh
# Progress: data/backfill_checkpoint.json (per-chunk confirmed counts),
#           logs/backfill.log (worker), logs/supervisor.log (supervisor)
# Single runner: the worker holds data/backfill.lock and steals it if the holder
# pid is dead, so re-running this after a kill resumes safely.
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 1
mkdir -p logs

# The token must be exported explicitly. It used to be harvested from
# Desktop/Cloudflare.txt -- a plaintext credential on the desktop. Never commit or echo it.
if [ -z "${CF_D1_TOKEN:-}" ]; then
  echo "FATAL: CF_D1_TOKEN is not set. Export it (resolve one with" >&2
  echo "       scripts/cf_deploy_token.py) before running a backfill." >&2
  exit 1
fi

if pgrep -f "backfill_supervisor.py" >/dev/null 2>&1; then
  echo "supervisor already running ($(pgrep -f backfill_supervisor.py | tr '\n' ' ')) — not starting a second."
else
  nohup python -u scripts/backfill_supervisor.py >> logs/supervisor.log 2>&1 &
  echo "supervisor started detached: pid=$!"
fi

echo "  checkpoint : $REPO/data/backfill_checkpoint.json"
echo "  worker log : $REPO/logs/backfill.log"
echo "  superv log : $REPO/logs/supervisor.log"
echo "  status     : python scripts/backfill_d1.py --status"