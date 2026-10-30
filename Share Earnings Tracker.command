#!/bin/bash
# Double-click to run the tracker and share it read-only with your team over Tailscale.
# Keeps this Mac awake while the window is open (closing the lid still sleeps it unless on power with a display).
cd "$(dirname "$0")" || exit 1
PY=python3
command -v "$PY" >/dev/null 2>&1 || { echo "python3 not found. Install Xcode Command Line Tools: xcode-select --install"; read -r; exit 1; }
if ! "$PY" -c "import requests, pypdf" >/dev/null 2>&1; then
  echo "Installing requests and pypdf…"
  "$PY" -m pip install --user -r requirements.txt || { echo "pip install failed"; read -r; exit 1; }
fi
TS=/Applications/Tailscale.app/Contents/MacOS/Tailscale
if [ -x "$TS" ]; then
  NAME=$("$TS" status --json 2>/dev/null | "$PY" -c "import json,sys; print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))" 2>/dev/null)
  [ -n "$NAME" ] && echo "Share this link with your team (they need Tailscale too): http://$NAME:8765/"
else
  echo "Tailscale is not installed: others can only reach this on the same network."
fi
exec caffeinate -i "$PY" earnings_tracker.py --share "$@"
