#!/usr/bin/env bash

# Usage:
#   ./add_ipv6_route_ll.sh <IF> <DST> <MET>
#
# Example:
#   ./add_ipv6_route_ll.sh vl_sat3_1 2001:db8:100::3/128 200

set -e

# LOCK_FILE="/tmp/add_ipv6_route_ll.lock"
# exec 9>"$LOCK_FILE"
# if ! flock -w 30 9; then
#     echo "Could not acquire lock: $LOCK_FILE"
#     exit 1
# fi

IF="$1"
DST="$2"
MET="$3"

if [ -z "$IF" ] || [ -z "$DST" ] || [ -z "$MET" ]; then
    echo "Usage: $0 <IF> <DST> <MET>"
    exit 1
fi

# Check interface exists
if ! ip link show "$IF" >/dev/null 2>&1; then
    echo "Interface $IF does not exist"
    exit 1
fi

PROBE_TRIES=30
PROBE_SLEEP_SEC=0.02

# echo "Discovering link-local neighbor on $IF ..."

# If a link-local neighbor is already cached on this interface, reuse it and skip probing.
NHLL=$(ip -6 neigh show dev "$IF" 2>/dev/null \
    | awk '/^fe80:/ {print $1; exit}')

# Try up to PROBE_TRIES times
if [ -z "$NHLL" ]; then
    for _ in $(seq 1 "$PROBE_TRIES"); do
        # Trigger NDP activity via link-local multicast, independent of reverse routing
        ping -6 -n -c1 -W1 -I "$IF" "ff02::1%$IF" >/dev/null 2>&1 || true
        # Try to extract first fe80:: neighbor
        NHLL=$(ip -6 neigh show dev "$IF" 2>/dev/null \
            | awk '/^fe80:/ {print $1; exit}')

        if [ -n "$NHLL" ]; then  
            break
        fi
        sleep "$PROBE_SLEEP_SEC"
    done
fi

if [ -z "$NHLL" ]; then
    echo "No link-local neighbor discovered on $IF, cannot add route to $DST"
    exit 1
fi

ip -6 route replace "$DST" via "$NHLL" dev "$IF" metric "$MET"
