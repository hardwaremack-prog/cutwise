#!/usr/bin/env bash
# Cutwise launcher for Linux / macOS. Needs python3 and ffmpeg installed.
cd "$(dirname "$0")"
command -v ffmpeg >/dev/null || { echo "Please install ffmpeg first (e.g. sudo apt install ffmpeg)"; exit 1; }
if [ ! -x .venv/bin/python ]; then python3 -m venv .venv; fi
if [ ! -f .venv/.ready ]; then
  .venv/bin/pip install -q --upgrade pip && .venv/bin/pip install -q -r requirements.txt && touch .venv/.ready
fi
exec .venv/bin/python server.py
