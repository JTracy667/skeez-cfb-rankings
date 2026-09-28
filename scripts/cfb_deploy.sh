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
  # The container app image update is EVENTUALLY CONSISTENT and can lag by MINUTES, not
  # seconds. On 2026-09-28 this function printed "ROLLBACK COMPLETE" while the container app
  # still pointed at the NEW tag: the rollback had not landed at all, and the page checks
  # above (all 200) happily agreed. Confirm the app image before claiming anything.
  if python "$(dirname "$0")/verify_container_swap.py" --tag "$1" --app-image-only \
       --window "${CFB_ROLLBACK_WINDOW:-600}"; then
    echo "!!! ROLLBACK COMPLETE (prod on $1, container app image confirmed). Investigate before retrying."
  else
    echo "!!! ROLLBACK NOT CONFIRMED: the container app image is NOT $1."
    echo "!!!   Prod may still be running the release you just tried to back out."
    echo "!!!   Do NOT assume the rollback took -- check the app image AND the code marker."
    return 1
  fi
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
# A non-flag in the documented FLAG slot is the code marker: `cfb_deploy.sh v47 v47-marker`
# is the natural 2-arg form, and treating it as the (unrecognised) flag silently dropped
# the marker and verified on the tag alone.
if [ -n "$NO_VERIFY" ] && [ "${NO_VERIFY#-}" = "$NO_VERIFY" ]; then
  EXPECT_CODE="$NO_VERIFY"; NO_VERIFY=""
fi
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

# 5b. THE IMAGE-APPLICATION GATE.
# `wrangler deploy` can print the image diff and report success while only the Worker's env
# moved: /api/health then says build=vN while the container APPLICATION still points at the
# old image, and the site serves old code forever. (v52->v53 sat exactly like that for an
# hour; a second `wrangler deploy` applied it immediately.) The app config is also EVENTUALLY
# CONSISTENT and can lag by MINUTES -- a 120s window once produced a FALSE rollback that
# itself did not land. So this polls, generously, rather than judging on one read. If the image never lands, deploy
# once more before letting the verify step fail and roll back.
if ! python "$(dirname "$0")/verify_container_swap.py" --tag "$NEW" --app-image-only --window "${CFB_IMAGE_WINDOW:-420}"; then
  echo "    container app did not take $NEW -> re-running wrangler deploy"
  wrangler_deploy
  python "$(dirname "$0")/verify_container_swap.py" --tag "$NEW" --app-image-only --window "${CFB_IMAGE_WINDOW_RETRY:-600}" \
    || { rollback "$PREV" "container app never moved to $NEW"; exit 1; }
fi

if [ "$NO_VERIFY" = "--no-verify" ]; then
  echo "deployed; verification skipped (--no-verify)."
  echo "verify manually once the warm instance recycles:"
  echo "  curl -s $PUBLIC_URL/api/health   # wait until \"build\":\"$NEW\""
  exit 0
fi

# 6. VERIFY live — the new image only answers once the warm instance retires.
#
# DO NOT REPLACE THIS WITH A SITE-POLLING LOOP. The previous loop curled /api/health every
# 6m against a 5m sleepAfter: the verification competed with the recycle it was waiting for.
# Any extra request inside that window -- a person checking the site, a monitor, or an agent
# asking "is it live yet" -- reset the idle timer and stalled the deploy indefinitely, while
# the loop happily burned its full budget. It also asserted `build`, which the Worker reports
# from its OWN config: a warm old instance answers `build: v53` while running v52 code.
# scripts/verify_container_swap.py watches the CONTAINERS API for the old instance to retire
# and then makes exactly ONE site request, asserting the CODE marker.
echo "=== verifying live build ==="
echo "    API-driven: waits for the instance to retire, then ONE request. DO NOT curl the site."
if ! python "$(dirname "$0")/verify_container_swap.py" \
      --tag "$NEW" --marker "${EXPECT_CODE:-$NEW}" \
      --attempts 3 --window "${CFB_VERIFY_WINDOW:-420}"; then
  rollback "$PREV" "container never came up on $NEW / ${EXPECT_CODE:-?} within the verification window"
  exit 1
fi

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
