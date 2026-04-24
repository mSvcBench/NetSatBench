#!/usr/bin/env bash
set -euo pipefail

SESSION_NAME="CONAGENT"
echo " 🚀 Launching connection-agent.py ..."

# default strategy
AGENT_CMD="python3 -u /app/usr/connection_agent_usr.py --measurement-top-n-links-strategy delay --handover-delay 80 --report"

# alternative
# AGENT_CMD="python3 -u /app/usr/connection_agent_usr.py --handover-delay 80 --report"

if screen -list | grep -qE "[0-9]+\\.${SESSION_NAME}[[:space:]]"; then
  echo "Existing ${SESSION_NAME} screen session found. Restarting it..."
  screen -S "${SESSION_NAME}" -X quit
  sleep 1
fi

screen -S "${SESSION_NAME}" -dm bash -lc "${AGENT_CMD}"
echo "Screen session '${SESSION_NAME}' started."
