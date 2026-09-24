#!/usr/bin/env bash
# CTO deploy path for skeezcfb-rankings.com (Cloudflare Worker + Container).
#
#   scripts/cfb_deploy.sh v16              # build -> preflight -> deploy -> verify
#   scripts/cfb_deploy.sh v16 --no-verify  # stop after deploy
#   scripts/cfb_deploy.sh --rollback v15   # re-point prod at a known-good tag
#
# WHY THIS EXISTS (incident 2026-09-21):
#   1. A deploy 500'd every page because the Dockerfile omitted a module app.py
#      imports. The image booted nowhere; prod was down until it was rolled back.
#      -> the PREFLIGHT boot gate now runs the exact image locally first.
#   2. "wrangler deploy succeeded" does NOT mean the change is live: a warm
#      container keeps serving the PREVIOUS image for up to sleepAfter (5m since the
#      sleepAfter shrink; it was 20m when that incident was written).
#      -> the VERIFY step polls /api/health until `build` equals the new tag,
#         and auto-rolls back to the previous tag on a 500 or a timeout.
#      -> `build` ALONE IS WEAK: it is an env var the Worker injects, so a warm
#         instance running the old image reports the new tag too. Pass the release's
#         code marker as the 3rd arg to make verification check the running CODE
#         (app.CODE_MARKER, surfaced as /api/health `code.marker`).
#
# Requires CLOUDFLARE_API_TOKEN + CLOUDFLARE_ACCOUNT_ID in the environment
# (never commit them). Reads nothing else; the D1 token lives in .env and in
# the Worker's own secrets.
set -uo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PUBLIC_URL="${CFB_PUBLIC_URL:-https://skeezcfb-rankings.com}"
WRANGLER="npx --yes wrangler@latest"
VERIFY_TIMEOUT="${CFB_VERIFY_TIMEOUT:-5400}"   # 90m: needs at least 2 full warm windows
# CRITICAL: the probe interval MUST exceed the container's sleepAfter.
# Probing more often than that keeps the instance ACTIVE, so it never sleeps,
# never recycles, and the new image can never come live -> false rollback.
# NOTE when you CHANGE sleepAfter: the interval that matters is the OUTGOING image's
# value, because the instance that has to idle out is the one ALREADY RUNNING. So the
# first deploy after shrinking sleepAfter still needs the OLD interval — pass it
# explicitly (e.g. CFB_VERIFY_INTERVAL=1260 for one run), then lower the default.
# sleepAfter is now 5m in src/index.js, so 360 is the steady-state value.
VERIFY_INTERVAL="${CFB_VERIFY_INTERVAL:-360}"   # 6m: exceeds the live image's 5m sleepAfter

die() { echo "ERROR: $*" >&2; exit 1; }

[ -n "${CLOUDFLARE_API_TOKEN:-}" ] || die "CLOUDFLARE_API_TOKEN is not set"
[ -n "${CLOUDFLARE_ACCOUNT_ID:-}" ] || die "CLOUDFLARE_ACCOUNT_ID is not set"

current_tag() { grep -oE 'cfb-power-rankings:v[0-9]+' wrangler.jsonc | head -1 | sed 's/.*://'; }

set_tag() {   # set_tag v16
  python - "$1" <<'PY'
import re, sys
tag = sys.argv[1]
p = 'wrangler.jsonc'
s = open(p, encoding='utf-8').read()
new = re.sub(r'(cfb-power-rankings:)v[0-9]+', r'\g<1>' + tag, s, count=1)
if new == s and f'cfb-power-rankings:{tag}' not in s:
    raise SystemExit('could not set image tag in wrangler.jsonc')
open(p, 'w', encoding='utf-8', newline='').write(new)
print(f'wrangler.jsonc image -> {tag}')
PY
}

wrangler_deploy() { $WRANGLER deploy 2>&1 | grep -E 'image|SUCCESS|Version ID' || true; }

rollback() {   # rollback <tag> <reason>
  echo
  echo "!!! ROLLING BACK to $1 — $2"
  set_tag "$1" || die "rollback could not rewrite the tag"
  wrangler_deploy
  echo "=== post-rollback pages ==="
  for p in "" analytics schedule win-totals api/health; do
    printf '   /%-12s %s\n' "$p" "$(curl -s -m 60 -o /dev/null -w '%{http_code}' -A 'Mozilla/5.0' "$PUBLIC_URL/$p")"
  done
  echo "!!! ROLLBACK COMPLETE (prod on $1). Investigate before retrying."
}

# ---------------------------------------------------------------- rollback mode
if [ "${1:-}" = "--rollback" ]; then
  T="${2:?usage: cfb_deploy.sh --rollback vN}"
  rollback "$T" "manual rollback requested"
  exit 0
fi

NEW="${1:?usage: cfb_deploy.sh vN [--no-verify] [code-marker]}"
NO_VERIFY="${2:-}"
# Optional 3rd arg: the CODE MARKER the new image must report at /api/health
# (`code.marker`). WHY: `build` is an env var the Worker INJECTS into whatever instance
# answers, so a warm container still running the PREVIOUS image happily reports the new
# tag — a deploy can "verify" against code that never shipped. Passing the marker makes
# the verify loop wait for the running CODE, not for the tag.
EXPECT_CODE="${3:-}"
[ -n "$EXPECT_CODE" ] || echo "note: no code marker given — 'build' alone cannot prove the new image is live"
PREV="$(current_tag)"
[ "$NEW" != "$PREV" ] || echo "note: tag already $NEW (rebuilding the same tag)"

echo "=== deploy $PREV -> $NEW  ($(date '+%H:%M:%S')) ==="

# 1. point the config at the new tag
set_tag "$NEW" || die "tag rewrite failed"

# 2. build + push the image from THIS working tree
echo "=== build + push cfb-power-rankings:$NEW ==="
$WRANGLER containers build . --tag "cfb-power-rankings:$NEW" --push 2>&1 | tail -3 \
  || { set_tag "$PREV"; die "image build/push failed (config left on $PREV)"; }

# 3. PREFLIGHT — boot the exact image locally. This is the gate that would have
#    stopped the 2026-09-21 outage.
echo "=== preflight boot gate ==="
if ! bash scripts/cfb_preflight_image.sh "cfb-power-rankings:$NEW"; then
  set_tag "$PREV"
  die "PREFLIGHT FAILED — nothing was deployed; config restored to $PREV"
fi

# 4. stamp the build so the verifier can prove which image is serving
printf '%s' "$NEW" | $WRANGLER secret put BUILD_TAG >/dev/null 2>&1 \
  && echo "BUILD_TAG -> $NEW" || echo "warning: could not set BUILD_TAG"

# 5. deploy
echo "=== wrangler deploy ==="
wrangler_deploy

if [ "$NO_VERIFY" = "--no-verify" ]; then
  echo "deployed; verification skipped (--no-verify)."
  echo "verify manually once the warm instance recycles:"
  echo "  curl -s $PUBLIC_URL/api/health   # wait until \"build\":\"$NEW\""
  exit 0
fi

# 6. VERIFY live — the new image only answers once the warm instance recycles.
echo "=== verifying live build ==="
echo "    a warm container can serve $PREV for up to ~5m; polling every $((VERIFY_INTERVAL/60))m, up to $((VERIFY_TIMEOUT/60))m"
deadline=$(( $(date +%s) + VERIFY_TIMEOUT ))
while :; do
  body=$(curl -s -m 30 -A 'Mozilla/5.0' "$PUBLIC_URL/api/health" || true)
  code=$(curl -s -m 30 -o /dev/null -w '%{http_code}' -A 'Mozilla/5.0' "$PUBLIC_URL/api/health" || true)
  if [ "$code" != "200" ]; then
    rollback "$PREV" "prod returned HTTP $code during verification"
    exit 1
  fi
  build=$(printf '%s' "$body" | python -c "import sys,json;print((json.load(sys.stdin) or {}).get('build',''))" 2>/dev/null)
  code_marker=$(printf '%s' "$body" | python -c "import sys,json;d=json.load(sys.stdin) or {};print(((d.get('code') or {}).get('marker')) or '')" 2>/dev/null)
  if [ "$build" = "$NEW" ] && { [ -z "$EXPECT_CODE" ] || [ "$code_marker" = "$EXPECT_CODE" ]; }; then
    echo "    live build == $NEW at $(date '+%H:%M:%S')"
    [ -n "$EXPECT_CODE" ] && echo "    live code == $code_marker (running CODE proven live, not just the tag)"
    break
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    rollback "$PREV" "new image never came live within $((VERIFY_TIMEOUT/60))m (last build='$build' code='$code_marker', wanted code='$EXPECT_CODE')"
    exit 1
  fi
  sleep "$VERIFY_INTERVAL"
done

echo "=== final page check on $NEW ==="
fail=0
for p in "" analytics schedule win-totals api/health; do
  c=""
  # A single transient failure must NOT roll back a good deploy. The container is
  # often still settling immediately after an image swap: on the v23 rollout this
  # check saw /schedule -> 000 and rolled back a build that was already live and
  # verified, while the same page answered 200 in 0.14s moments later.
  # A checker that cries wolf is worse than no checker — retry before judging.
  for attempt in 1 2 3 4 5; do
    c=$(curl -s -m 45 -o /dev/null -w '%{http_code}' -A 'Mozilla/5.0' "$PUBLIC_URL/$p")
    [ "$c" = "200" ] && break
    echo "   /$p -> $c (attempt $attempt/5) — retrying in 10s"
    sleep 10
  done
  printf '   /%-12s %s\n' "$p" "$c"
  [ "$c" = "200" ] || fail=1
done
[ "$fail" = "0" ] && echo "DEPLOY VERIFIED LIVE: $NEW" || { rollback "$PREV" "a public page was still not 200 after 5 attempts"; exit 1; }
