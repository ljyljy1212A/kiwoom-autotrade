#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON_EXE="${PYTHON_EXE:-python3}"
if ! command -v "$PYTHON_EXE" >/dev/null 2>&1; then
  echo "Python runtime was not found: $PYTHON_EXE"
  echo "Install the project dependencies with that Python runtime, then run this file again."
  exit 1
fi
if [ ! -f .env ]; then
  echo "Missing .env. Copy .env.example to .env and add your mock credentials first."
  exit 1
fi

mkdir -p logs
nohup "$PYTHON_EXE" dashboard/dashboard_server.py > logs/dashboard_server.log 2>&1 &
sleep 2
if command -v xdg-open >/dev/null 2>&1; then
  xdg-open http://127.0.0.1:8765 >/dev/null 2>&1 &
fi
