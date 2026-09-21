"""Kill every stale backfill_supervisor.py process (there were 7 duplicates from
concurrent cron fires), leaving the repo ready for exactly ONE supervisor.
Run: python kill_supervisors.py
"""
import csv, io, os, subprocess, sys

PS = ["powershell", "-NoProfile", "-Command",
      "$ErrorActionPreference='SilentlyContinue'; "
      "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
      "Where-Object { $_.CommandLine -match 'backfill_supervisor' } | "
      "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; "
      "Write-Output $_.ProcessId }"]
out = subprocess.run(PS, capture_output=True, text=True, timeout=120)
print("killed supervisor pids:", " ".join(w for w in out.stdout.split() if w.strip().isdigit()))

# also any live WORKER (a sleeping supervisor has none, but be sure)
PS2 = ["powershell", "-NoProfile", "-Command",
       "$ErrorActionPreference='SilentlyContinue'; "
       "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
       "Where-Object { $_.CommandLine -match 'backfill_d1' } | "
       "ForEach-Object { Write-Output ('WORKER-ALIVE ' + $_.ProcessId + ' ' + $_.CommandLine) }"]
out2 = subprocess.run(PS2, capture_output=True, text=True, timeout=120)
print("workers still alive:", out2.stdout.strip() or "none")

# report leftovers
PS3 = ["powershell", "-NoProfile", "-Command",
       "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
       "Where-Object { $_.CommandLine -match 'backfill' } | "
       "ForEach-Object { Write-Output ($_.ProcessId.ToString() + ' ' + $_.CommandLine) }"]
out3 = subprocess.run(PS3, capture_output=True, text=True, timeout=120)
print("leftover backfill procs:", out3.stdout.strip() or "none")
