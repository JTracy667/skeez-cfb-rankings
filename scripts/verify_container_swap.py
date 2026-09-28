#!/usr/bin/env python3
"""Wait for a Cloudflare Container swap, then verify the RUNNING CODE once.

WHY THIS EXISTS
---------------
The old verify loop polled the SITE every 6 minutes. `sleepAfter` is 5 minutes, so every
poll reset the container's idle timer: the verification competed with the very recycle it
was waiting for. Any extra request inside that window -- a person checking the site, a
monitor, or an agent asking "is it live yet" -- could stall a deploy indefinitely, and the
loop could burn its whole budget while never observing the new code. Worse, the thing it
asserted was `build`, which the Worker reports from ITS OWN config: while the old instance
is still warm, `/api/health` cheerfully says `build: v53` while serving v52 code.

HOW IT WORKS
------------
1. Watch the CONTAINERS API only (never the site) until the instance reports `inactive`,
   i.e. the old instance has actually retired.
2. Make exactly ONE site request. That boots a fresh instance from the current app config.
3. Assert the CODE MARKER, not `build`. `build` is the Worker's tag and it lies; the marker
   is baked into the image that is actually running.
4. If the marker is still old, wait for another idle window and try again, bounded.

Usage:
  python scripts/verify_container_swap.py --tag v53 --marker v53-schedule-in-d1

Exit 0 = verified live. 1 = not verified (reason printed). 2 = config error.
"""
import argparse
import json
import os
import sys
import time
import urllib.request

ACCOUNT = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "90c2c31beec12cb7de1c249ade1eb773")
APP = os.environ.get("CF_CONTAINER_APP", "a039e361-4419-451f-ad0a-56464dcc0f65")
SITE = "https://skeezcfb-rankings.com"


def app_image():
    """The image the container APPLICATION is configured to run. No site contact."""
    r = cf_api("")["result"]
    return (r.get("configuration") or {}).get("image") or "", r.get("version")


def wait_for_app_image(tag, seconds, poll=15):
    """Phase 0: the app config is what decides which image runs, and `wrangler deploy` can
    report SUCCESS while only the Worker's env moved (`build` says vN, the app still points at
    the previous image, and the site serves old code forever). It is also EVENTUALLY
    CONSISTENT -- a read straight after the deploy can still show the old image. Poll the API
    (never the site) until it lands."""
    deadline = time.time() + seconds
    last = None
    while time.time() < deadline:
        img, ver = app_image()
        cur = f"app image {img.rsplit(':', 1)[-1]} (version {ver})"
        if cur != last:
            print(time.strftime("  [%H:%M:%S] ") + cur)
            last = cur
        if img.rsplit(":", 1)[-1] == tag:
            return True
        time.sleep(poll)
    return False


def cf_api(path):
    tok = os.environ.get("CLOUDFLARE_API_TOKEN")
    if not tok:
        sys.exit("config error: CLOUDFLARE_API_TOKEN not set "
                 "(resolve it with scripts/cf_deploy_token.py)")
    url = (f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}"
           f"/containers/applications/{APP}" + (f"/{path}" if path else ""))
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + tok})
    return json.load(urllib.request.urlopen(req, timeout=60))


def container_state():
    """(image_tag, state) for the singleton instance. No site contact."""
    inst = cf_api("instances")["result"]["instances"]
    if not inst:
        return None, "no-instance"
    i = inst[0]
    return (i.get("image") or "").rsplit(":", 1)[-1], (i.get("status") or {}).get("state")


def wait_for_idle(seconds, poll=20):
    """Wait until the instance retires. API-only, so this cannot keep it warm."""
    deadline = time.time() + seconds
    last = None
    while time.time() < deadline:
        img, state = container_state()
        cur = f"image {img} | state {state}"
        if cur != last:
            print(time.strftime("  [%H:%M:%S] ") + cur)
            last = cur
        if state in ("inactive", None, "no-instance"):
            return img, state
        time.sleep(poll)
    return container_state()


def one_site_check():
    """A single request. Deliberately not a loop."""
    req = urllib.request.Request(SITE + "/api/health",
                                headers={"User-Agent": "Mozilla/5.0"})
    h = json.load(urllib.request.urlopen(req, timeout=90))
    return h.get("build"), (h.get("code") or {}).get("marker"), h.get("status")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True, help="image tag that should be running, e.g. v53")
    ap.add_argument("--marker", default="", help="CODE_MARKER baked into that image")
    ap.add_argument("--app-image-only", action="store_true",
                    help="check ONLY that the container app is configured for --tag, then exit "
                         "(used by cfb_deploy.sh to catch a `wrangler deploy` that reported "
                         "success without applying the image, before deciding to retry)")
    ap.add_argument("--attempts", type=int, default=3)
    ap.add_argument("--window", type=int, default=420,
                    help="seconds to wait for an idle window, per attempt (must exceed sleepAfter)")
    a = ap.parse_args()

    print(f"target: image {a.tag} / code marker {a.marker}")
    print("DO NOT TOUCH THE SITE while this runs: every request resets the idle timer\n"
          "and this tool waits on the Containers API precisely so it does not stall itself.")

    # PHASE 0: `wrangler deploy` can report SUCCESS while the CONTAINER APP still points at
    # the previous image -- then /api/health says build=vN, the site serves old code forever,
    # and every downstream check inspects the wrong thing. Confirm the app config first.
    print("phase 0: confirming the container app is configured for", a.tag)
    if not wait_for_app_image(a.tag, a.window):
        cur, ver = app_image()
        print(f"NOT VERIFIED: container app is still on {cur} (version {ver}), wanted {a.tag}.\n"
              "  `wrangler deploy` did not apply the container image change -- re-run it and\n"
              "  watch for 'SUCCESS Modified application'.")
        return 1
    if a.app_image_only:
        print(f"APP IMAGE OK: {a.tag}")
        return 0

    for n in range(1, a.attempts + 1):
        print(f"attempt {n}/{a.attempts}: waiting for the old instance to retire")
        img, state = wait_for_idle(a.window)
        print(f"  idle window secured (image {img}, state {state}) -> ONE site request")
        try:
            build, marker, status = one_site_check()
        except Exception as e:  # noqa: BLE001
            print(f"  request failed: {e}")
            continue
        print(f"  site: build {build} | marker {marker} | status {status}")
        if marker == a.marker:
            print(f"VERIFIED LIVE: {a.tag} / {a.marker}")
            return 0
        print(f"  not yet: marker is {marker!r}, expected {a.marker!r}"
              f"{' (build lies while the old instance is warm)' if build == a.tag else ''}")
    print(f"NOT VERIFIED after {a.attempts} attempts: {a.tag} / {a.marker}")
    return 1


if __name__ == "__main__":
    sys.exit(main())