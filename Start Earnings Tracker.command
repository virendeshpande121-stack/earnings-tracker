#!/bin/bash
# Double-click to start the Alphapeak Earnings Tracker (add --demo to try it offline).
cd "$(dirname "$0")" || exit 1
PY=python3
command -v "$PY" >/dev/null 2>&1 || { echo "python3 not found. Install Xcode Command Line Tools: xcode-select --install"; read -r; exit 1; }
if ! "$PY" -c "import requests" >/dev/null 2>&1; then
  echo "Installing requests…"
  "$PY" -m pip install --user -r requirements.txt || { echo "pip install failed"; read -r; exit 1; }
fi
"$PY" earnings_tracker.py "$@"
