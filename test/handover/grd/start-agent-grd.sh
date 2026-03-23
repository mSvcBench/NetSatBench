#!/usr/bin/env bash
echo " 🚀 Launching connection-agent.py ..."
/usr/bin/screen -S CONAGENT -s /bin/bash -t win0 -A -d -m
sleep 1
screen -S CONAGENT -p win0 -X stuff $'python3 -u /app/grd/connection_agent_grd.py --handover-delay 80 --log-level DEBUG\n'
