#!/usr/bin/env bash
# CTO pre-deploy boot gate for the CFB container image.
#
# WHY: on 2026-09-21 a deploy 500'd every page with
#   "Failed to start container: The container is not running"
# because the Dockerfile did not COPY a newly-added module that app.py imports
# (cfbd_shared.py / d1_write_path.py). The container died at import, so the
# Worker had nothing to serve. A local boot of the exact image catches that
# whole failure class in ~30s, before prod is touched, at zero cost.
#
# It also catches the second half of the same incident: BUILD_TAG must be
# present and echo back, so a post-deploy check can tell a NEW image apart from
# a warm instance of the previous one.
#
# Usage: scripts/cfb_preflight_image.sh cfb-power-rankings:v16 [expected-build]
set -u

IMG="${1:?usage: cfb_preflight_image.sh <repo/image:tag> [expected-build]}"
EXPECT="${2:-}"
NAME="cfb-preflight"
PORT="${CFB_PREFLIGHT_PORT:-18003}"

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

if ! docker image inspect "$IMG" >/dev/null 2>&1; then
  echo "FAIL: image not present locally: $IMG"
  echo "      build it first: npx wrangler containers build . --tag $IMG --push"
  exit 1
fi

echo "== booting $IMG on :$PORT =="
docker run -d --name "$NAME" -p "${PORT}:8003" "$IMG" >/dev/null || {
  echo "FAIL: docker run refused the image"; exit 1; }

up=0
for _ in $(seq 1 30); do
  code=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/api/health" || true)
  [ "$code" = "200" ] && { up=1; break; }
  if ! docker ps --filter "name=$NAME" --filter status=running -q | grep -q .; then
    break
  fi
  sleep 2
done

if [ "$up" != "1" ]; then
  echo "FAIL: container never served /api/health — DO NOT DEPLOY."
  echo "--- last 40 log lines ---"
  docker logs --tail 40 "$NAME" 2>&1
  exit 1
fi

echo "== every public page =="
fail=0
for p in "" analytics schedule win-totals api/health api/rankings; do
  code=$(curl -s -m 15 -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/${p}")
  printf '   /%-12s %s\n' "$p" "$code"
  [ "$code" = "200" ] || fail=1
done

echo "== build stamp =="
build=$(curl -s -m 10 "http://127.0.0.1:${PORT}/api/health" \
        | python -c "import sys,json;print((json.load(sys.stdin) or {}).get('build',''))" 2>/dev/null)
echo "   BUILD_TAG -> '${build}'"
if [ -n "$EXPECT" ] && [ "$build" != "$EXPECT" ]; then
  echo "FAIL: image reports build='${build}', expected '${EXPECT}'"
  exit 1
fi
if [ "$build" = "dev" ]; then
  echo "   note: BUILD_TAG not supplied (expected for a bare local boot)"
fi

[ "$fail" = "0" ] && echo "PREFLIGHT PASS" || { echo "PREFLIGHT FAIL"; exit 1; }
