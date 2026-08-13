#!/usr/bin/env python3
"""Start the FastAPI server on port 8003."""
import subprocess, sys, os

os.chdir('C:/Users/JeffTracy/Desktop/cfb-power-rankings')
proc = subprocess.Popen(
    [sys.executable, '-m', 'uvicorn', 'app:app', '--host', '0.0.0.0', '--port', '8003'],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)
print(f"Server started, PID={proc.pid}")
