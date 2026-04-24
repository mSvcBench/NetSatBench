#!/usr/bin/env bash
set -euo pipefail

SESSION_NAME="CONAGENT"
echo " 🚀 Launching connection-agent.py ..."

# max-rate, coupled
AGENT_CMD="python3 -u /app/grd/connection_agent_grd.py --handover-delay 80 --walker-star --report --grd-handover-filters min_duration,min_orbit_hops,max_rate --user-handover-filters min_duration,min_orbit_hops,max_rate --handover-mode 2"

# max visibility, coupled
# AGENT_CMD="python3 -u /app/grd/connection_agent_grd.py --handover-delay 80 --walker-star --report --grd-handover-filters min_duration,min_orbit_hops,longest_duration --user-handover-filters min_duration,min_orbit_hops,longest_duration"

# best-delay, coupled
# AGENT_CMD="python3 -u /app/grd/connection_agent_grd.py --handover-delay 80 --walker-star --report --grd-handover-filters min_duration,min_orbit_hops,min_delay --user-handover-filters min_duration,min_orbit_hops,min_delay --handover-mode 2"

# max visibility, change only when remaining visibility duration is lower than 60s (default lifetime_threshold_s).
# AGENT_CMD="python3 -u /app/grd/connection_agent_grd.py --handover-delay 80 --walker-star --report --grd-handover-filters min_duration,longest_duration --user-handover-filters min_duration,longest_duration --handover-mode 1 --handover-hold-period 3600"

# min delay, change only when new access satellite with lower delay is available
# AGENT_CMD="python3 -u /app/grd/connection_agent_grd.py --handover-delay 80 --walker-star --report --grd-handover-filters min_delay --user-handover-filters min_delay --handover-mode 1 --handover-hold-period 15"

if screen -list | grep -qE "[0-9]+\\.${SESSION_NAME}[[:space:]]"; then
  echo "Existing ${SESSION_NAME} screen session found. Restarting it..."
  screen -S "${SESSION_NAME}" -X quit
  sleep 1
fi

# Launch directly with the target command so the session doesn't exit before `stuff` is sent.
screen -S "${SESSION_NAME}" -dm bash -lc "${AGENT_CMD}"
echo "Screen session '${SESSION_NAME}' started."
