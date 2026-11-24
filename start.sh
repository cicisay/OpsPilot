#!/usr/bin/env bash
# OpsPilot console - one-click launcher (macOS / Linux)
# Usage:
#   ./start.sh          -> demo mode (read-only preview, publish disabled)
#   ./start.sh full     -> normal mode (approval + publish enabled)

set -u
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "[ERROR] python3 not found. Please install Python 3.11 or later first."
  exit 1
fi

if [ ! -f "data/console/users.json" ]; then
  echo "[1/2] First run: creating console data and demo accounts..."
  python3 run.py console init \
    --admin-password "Demo12345" \
    --approver-password "Approve12345" \
    --operator-password "Operate12345" || { echo "[ERROR] Init failed."; exit 1; }
else
  echo "[1/2] Console data found, skipping init."
fi

MODE=demo
RUN_ARGS="--demo"
if [ "${1:-}" = "full" ]; then
  MODE=full
  RUN_ARGS=""
fi

echo "[2/2] Starting OpsPilot console ($MODE mode) on http://127.0.0.1:8788 ..."
echo
echo "============================================================"
echo "  Console    : http://127.0.0.1:8788"
echo "  Accounts   : admin / approver / operator"
echo "  First run  : admin / Demo12345"
echo "               approver / Approve12345"
echo "               operator / Operate12345"
echo "  Mode       : $MODE"
echo "  Stop       : press Ctrl+C in this window"
echo "============================================================"
echo

( sleep 2; xdg-open "http://127.0.0.1:8788" >/dev/null 2>&1 || open "http://127.0.0.1:8788" >/dev/null 2>&1 || true ) &

python3 run.py console serve --host 127.0.0.1 --port 8788 $RUN_ARGS
