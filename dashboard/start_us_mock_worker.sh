#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON_EXE="${PYTHON_EXE:-python3}"
if ! command -v "$PYTHON_EXE" >/dev/null 2>&1; then
  echo "Python runtime was not found: $PYTHON_EXE"
  exit 1
fi

export AUTO_TRADING_ENABLED=false
exec "$PYTHON_EXE" -m src.worker_supervisor start --account us_mock --market US
