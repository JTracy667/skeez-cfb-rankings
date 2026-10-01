# Serve the CFB candidate LOCALLY so a candidate commit can be exercised as a served site.
#
# WHY THIS EXISTS: `verify_pages_live.mjs` pointed at the hardcoded production URL, so its
# receipts only ever described what was DEPLOYED -- never the candidate under review. Pair this
# with `CFB_BASE_URL=http://127.0.0.1:<port> node scripts/verify_pages_live.mjs` for a
# candidate-level page/API receipt.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/serve_candidate_local.ps1
#
# OFFLINE AND TARGET-ISOLATED BY CONSTRUCTION. A reviewer does not need a promise that this
# server will not write; they need a reason it CANNOT:
#
#   1. NO D1 CREDENTIAL EXISTS IN THE PROCESS. The variables are REMOVED, not set to sentinels.
#      A sentinel only proves what the environment claims; with the variables genuinely absent,
#      d1_store._token() raises and every store path fails closed. /api/health therefore reports
#      candidate.d1.token_present = false, and a reviewer can ASSERT that from the response
#      instead of trusting this comment. (Absence is safe HERE because the repo `.env` holds only
#      provider keys -- CFBD and The Odds API -- never D1 ones. app.py:47 loads `.env` at import,
#      so a D1 key living there would be refilled; if one is ever added, revisit this script.)
#   2. THE DATA DIRECTORY IS A SCRATCH COPY, so the repo's data/ cannot change.
#   3. METERED PROVIDERS GET SENTINELS, not absence: `.env` DOES hold those keys, so removing
#      them would let the loader refill them and the candidate would spend real quota.
#   4. ADMIN_TOKEN is random and undisclosed, so the ops POST routes are not callable.
#
# WHY NOT JUST THE WRITE FLAG: `D1_WRITE_ENABLED=0` does not cover every write path. The
# api_usage metering flush (budget.py -> d1_store.upsert_api_usage) ignored it and wrote 8 rows
# to PRODUCTION D1 from a local instance during round 5. That path is gated now, but the
# credential is what actually stops a call -- so it is absent rather than bogus.
#
# IDENTITY: CFB_BUILD_COMMIT is passed for context, but the proof is the source digest reported
# in /api/health (candidate.source_digest). A commit can be claimed; a digest can be recomputed
# from the checkout with `python candidate_identity.py`.
param(
  [int]$Port = 8011,
  [string]$DataDir = "$env:LOCALAPPDATA\hermes\profiles\cto\cache\scratch\candidate_serve\data"
)

$ErrorActionPreference = 'Continue'
Set-Location (Split-Path $PSScriptRoot -Parent)

$env:CFB_DATA_DIR        = $DataDir      # scratch COPY: the repo's data/ cannot change
$env:CFB_SKIP_LIVE_FETCH = '1'           # no metered provider calls
$env:CFB_SKIP_BOOTWARM   = '1'           # no boot-time refresh
$env:D1_WRITE_ENABLED    = '0'           # write_enabled() false (d1_write_path.py:31)
$env:CFB_READ_ONLY       = '1'           # reported in /api/health
$env:PYTHONUNBUFFERED    = '1'
$env:CFB_BUILD_COMMIT    = (git rev-parse HEAD)

$env:ADMIN_TOKEN          = [guid]::NewGuid().ToString('N')
$env:PROPLINE_KEY         = 'local-offline-no-fetch'
$env:CFBD_API_KEY         = 'local-offline-no-fetch'
$env:THE_ODDS_API_KEY     = 'local-offline-no-fetch'

# Tripwire FIRST, while the parent's value is still visible: the repo .env loader and d1_store's
# own default BOTH point at production, so targeting prod by accident is the default outcome.
if ($env:CF_D1_DB_ID -eq 'c3ec3149-cc85-483b-b727-5a18e3d5a1b9') {
  Write-Error 'refusing to serve: CF_D1_DB_ID points at PRODUCTION D1'
  exit 2
}

# D1 CREDENTIALS ARE SENTINELS, NOT MERELY REMOVED. Removing them is not enough: the repo `.env`
# holds keys the app loads at import (app.py:47), so ABSENCE gets refilled from that file -- the
# candidate came up holding a real (if revoked) token exactly this way. A present-but-useless
# value blocks the refill and is rejected by the API, so every store path fails closed.
# /api/health reports d1.token_is_placeholder = true, and d1.serve shows D1 was not used.
if ($env:CF_D1_TOKEN -or $env:CLOUDFLARE_API_TOKEN) {
  Write-Host 'note: a D1 credential was present in the parent environment; replacing it with a placeholder'
}
$env:CF_D1_TOKEN          = 'local-offline-no-store'
$env:CLOUDFLARE_API_TOKEN = 'local-offline-no-store'
$env:CF_D1_DB_ID          = ''

Write-Host "serving the working tree at http://127.0.0.1:$Port  (offline: no D1, no fetches)"
python -m uvicorn app:app --host 127.0.0.1 --port $Port