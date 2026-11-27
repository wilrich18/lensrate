#!/bin/bash
# Mac / Linux launcher: double-click (Mac) or run ./"Start Day Trader.command"
cd "$(dirname "$0")" || exit 1
PY=$(command -v python3 || command -v python)
if [ -z "$PY" ] || ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  echo "Python 3.9 or newer is needed. Get it from https://www.python.org/downloads/ then double-click this again."
  read -r -p "Press Enter to close..."; exit 1
fi
if [ ! -x .venv/bin/python ]; then
  echo "First run: setting things up (about a minute)..."
  "$PY" -m venv .venv || { read -r -p "Could not create the environment. Press Enter..."; exit 1; }
fi
.venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt || { read -r -p "Install failed (check internet). Press Enter..."; exit 1; }
echo "Starting Day Trader... your browser will open. Keep this window open while trading; close it to quit."
.venv/bin/python app.py --open
