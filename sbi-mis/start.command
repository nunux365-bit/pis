#!/bin/bash
# SBI MIS — double-click to launch
set -e
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

if [ ! -d ".venv" ]; then
  echo "First run — creating virtual environment..."
  python3 -m venv .venv
fi

source .venv/bin/activate

# Install/upgrade deps if missing or requirements.txt has changed
if [ ! -f ".venv/.deps-installed" ] || [ "requirements.txt" -nt ".venv/.deps-installed" ]; then
  echo "Installing dependencies..."
  pip install --quiet --upgrade pip
  pip install --quiet -r requirements.txt
  touch ".venv/.deps-installed"
fi

PORT=8765
LOG="$APP_DIR/sbi-mis.log"
PID_FILE="$APP_DIR/sbi-mis.pid"

echo ""
echo "Starting SBI MIS on http://localhost:$PORT ..."
echo ""

# Browser opens shortly after server binds (background server — closing this window does not stop it).
#(sleep 2 && open "http://localhost:$PORT") &

nohup python -m uvicorn backend.main:app --host 127.0.0.1 --port $PORT >>"$LOG" 2>&1 &
echo $! >"$PID_FILE"

echo "Running in background — PID $(cat "$PID_FILE")"
echo "Log file: $LOG"
echo "Stop the server with: kill \$(cat \"$PID_FILE\")"
echo ""
