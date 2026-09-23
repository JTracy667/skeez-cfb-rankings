#!/usr/bin/env bash
# Post-deploy quiet probe — one touch, only AFTER the idle window has fully elapsed.
#
# WHY: a Cloudflare Containers rollout does not swap the image instantly. The warm
# instance keeps serving the PREVIOUS image until it idles out (sleepAfter). Every
# request resets that idle timer, so probing sooner than sleepAfter does not "check
# early" — it GUARANTEES the old image keeps serving, and then the deploy verifier can
# auto-roll-back a perfectly good rollout. The window is 21 MINUTES MINIMUM with
# NOTHING touching the site: no health checks, no page curls, no watchdogs, no
# deploy-script loops, no ad-hoc peeks.
#
# This script exists so the wait cannot be skipped: it sleeps, then probes ONCE.
#
#   usage: postdeploy_quiet_probe.sh <wait_seconds> <out_dir>
#   e.g.   postdeploy_quiet_probe.sh 1320 \
#            "$LOCALAPPDATA/hermes/profiles/cto/cache/scratch/v30_probe"
#
# Exit code is the probe's own HTTP status (0 == all endpoints answered 200).

set -u
WAIT="${1:-1320}"
OUT="${2:-.}"
mkdir -p "$OUT"

# Sleep the FULL window first. Nothing below this line may run early.
sleep "$WAIT"

TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
{
  echo "probe_started_utc=$TS"
  echo "waited_seconds=$WAIT"
} > "$OUT/status.txt"

rc=0
for ep in api/health api/rankings api/schedule; do
  f="$OUT/${ep//\//_}.json"
  code=$(curl -s -m 45 -A 'Mozilla/5.0' -o "$f" -w '%{http_code}' \
           "https://skeezcfb-rankings.com/$ep" || echo 000)
  echo "$ep -> $code" >> "$OUT/status.txt"
  [ "$code" = "200" ] || rc=1
  # One probe per endpoint, spaced, so the instance is touched once per minute at most.
  sleep 20
done
for p in "" analytics schedule win-totals; do
  # NOTE: take only the first 3 chars. `curl -w` prints the code and THEN can still exit
  # non-zero (e.g. a write error), so `|| echo 000` appends to a printed "200" and the
  # status file reads "200000" — which looks like a failure when the page was fine.
  code=$(curl -s -m 45 -A 'Mozilla/5.0' -o /dev/null -w '%{http_code}' \
           "https://skeezcfb-rankings.com/$p" 2>/dev/null)
  code=${code:0:3}
  echo "/$p -> $code" >> "$OUT/status.txt"
  [ "$code" = "200" ] || rc=1
done
echo "probe_finished_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$OUT/status.txt"
exit "$rc"