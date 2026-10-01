# Serve the CFB candidate LOCALLY so a candidate commit can be exercised as a served site.
#
# WHY THIS EXISTS: `verify_pages_live.mjs` pointed at the hardcoded production URL, so its
# receipts only ever described what was DEPLOYED -- never the candidate under review. Pair this
# with `CFB_BASE_URL=http://127.0.0.1:<port> node scripts/verify_pages_live.mjs` for a
# candidate-level page/API receipt.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/serve_candidate_local.ps1
#
# OFFLINE BY CONSTRUCTION. Two traps found while building this, both silent:
#
#   1. app.py:47 loads the repository `.env` at import, so the production D1 credentials enter
#      the process no matter what the parent environment says. Unsetting them in the shell does
#      nothing.
#   2. On Windows, `$env:X = ''` makes X ABSENT in the child (verified), so app.py:59's
#      `key not in os.environ` guard would let `.env` refill it. Absence is not protection.
#
# So the credentials are set to present-but-useless SENTINELS: the key is set (the .env loader
# skips it) and every store call is rejected by the API, which drives the documented disk
# fallback. ADMIN_TOKEN is a random undisclosed value so the ops POST routes are not callable.
#
# THE SENTINEL IS THE GUARANTEE, NOT THE FLAG. `D1_WRITE_ENABLED=0` sets write_enabled() false,
# but that flag does NOT cover every write path: the api_usage metering flush (budget.py ->
# d1_store.upsert_api_usage) ignored it and wrote 8 rows to PRODUCTION D1 from a local instance
# during round 5. A round-5 fix gates that path too, but a valid credential is what actually
# stops a call, so keep the token bogus.
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
$env:PYTHONUNBUFFERED    = '1'

$env:CF_D1_TOKEN          = 'local-offline-no-store'
$env:CLOUDFLARE_API_TOKEN = 'local-offline-no-store'
$env:CF_D1_DB_ID          = 'local-offline-no-store'
$env:ADMIN_TOKEN          = [guid]::NewGuid().ToString('N')
$env:PROPLINE_KEY         = 'local-offline-no-fetch'
$env:CFBD_API_KEY         = 'local-offline-no-fetch'
$env:THE_ODDS_API_KEY     = 'local-offline-no-fetch'

# Tripwire: the repo .env loader and d1_store's own default BOTH point at production, so
# targeting prod by accident is the default outcome, not the exception. Refuse to serve at all.
if ($env:CF_D1_DB_ID -eq 'c3ec3149-cc85-483b-b727-5a18e3d5a1b9') {
  Write-Error 'refusing to serve: CF_D1_DB_ID points at PRODUCTION D1'
  exit 2
}

Write-Host "serving the working tree at http://127.0.0.1:$Port  (offline: no D1, no fetches)"
python -m uvicorn app:app --host 127.0.0.1 --port $Port