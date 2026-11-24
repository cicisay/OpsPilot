#!/usr/bin/env bash
# OpsPilot pipeline - one-click refresh (macOS / Linux)
# Sync site snapshot -> supervisor scan -> workshop proposals -> review,
# writing everything into the tenant dir the console actually reads.
# Prereq: trade site running on :3000, SITE_AGENT_TOKEN env var set.

set -euo pipefail
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "[ERROR] python3 not found."
  exit 1
fi
if [ -z "${SITE_AGENT_TOKEN:-}" ]; then
  echo "[ERROR] SITE_AGENT_TOKEN is not set in this shell."
  echo "        export SITE_AGENT_TOKEN=<token> and retry."
  exit 1
fi

T=data/tenants/globaltrade

echo "[1/4] Syncing site snapshot from http://localhost:3000/api/agent ..."
python3 run.py site sync --out "$T/site_snapshot.json"

echo "[2/4] Supervisor: scanning products, writing TaskOrders ..."
python3 run.py supervisor run --snapshot "$T/site_snapshot.json" --out "$T/task_orders.json"

echo "[3/4] Workshop: generating proposals ..."
python3 run.py workshop run --tasks "$T/task_orders.json" --snapshot "$T/site_snapshot.json" --out "$T/workshop_proposals.json"

echo "[4/4] Review: compliance check ..."
python3 run.py review run --proposals "$T/workshop_proposals.json" --snapshot "$T/site_snapshot.json" --out "$T/review_results.json"

echo
echo "============================================================"
echo "  Pipeline done. Refresh the OpsPilot console (F5) to see"
echo "  the new tasks and proposals."
echo "============================================================"
