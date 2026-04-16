#!/usr/bin/env bash
echo " 🚀 Launching connection-agent.py ..."
/usr/bin/screen -S CONAGENT -s /bin/bash -t win0 -A -d -m
# screen -S CONAGENT -p win0 -X stuff $'python3 -u /app/usr/connection_agent_usr.py --handover-delay 80 --report\n'
screen -S CONAGENT -p win0 -X stuff $'python3 -u /app/usr/connection_agent_usr.py --measurement-top-n-links-strategy delay --handover-delay 80 --report\n'