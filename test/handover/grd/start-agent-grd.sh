#!/usr/bin/env bash
echo " 🚀 Launching connection-agent.py ..."
/usr/bin/screen -S CONAGENT -s /bin/bash -t win0 -A -d -m
sleep 1
# best-lifetime
#screen -S CONAGENT -p win0 -X stuff $'python3 -u /app/grd/connection_agent_grd.py --handover-delay 80 --walker-star --report --grd-handover-filters min_duration,min_orbit_hops,load_balancing,longest_duration --user-handover-filters min_duration,min_orbit_hops,longest_duration\n'

# best-delay
screen -S CONAGENT -p win0 -X stuff $'python3 -u /app/grd/connection_agent_grd.py --handover-delay 80 --walker-star --report --grd-handover-filters min_duration,min_orbit_hops,load_balancing,min_delay --user-handover-filters min_duration,min_orbit_hops,min_delay\n'

# best-delay, no load balancing
#nscreen -S CONAGENT -p win0 -X stuff $'python3 -u /app/grd/connection_agent_grd.py --handover-delay 80 --walker-star --report --grd-handover-filters min_duration,min_orbit_hops,min_delay --user-handover-filters min_duration,min_orbit_hops,min_delay\n'
