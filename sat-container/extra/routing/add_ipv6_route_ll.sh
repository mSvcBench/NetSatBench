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
PROBE_SLEEP_SEC=1.0

# echo "Discovering link-local neighbor on $IF ..."

# If a link-local neighbor is already cached on this interface, reuse it and skip probing.
NHLL=$(ip -6 neigh show dev "$IF" 2>/dev/null \
    | awk '/^fe80:/ {print $1; exit}')

# Try up to PROBE_TRIES times
if [ -z "$NHLL" ]; then
    for _ in $(seq 1 "$PROBE_TRIES"); do
        attempt_start_ts="$EPOCHREALTIME"
        # Trigger NDP activity via link-local multicast, independent of reverse routing
        ping -6 -n -c1 -W1 -I "$IF" "ff02::1%$IF" >/dev/null 2>&1 || true
        ping_done_ts="$EPOCHREALTIME"
        # Try to extract first fe80:: neighbor
        NHLL=$(ip -6 neigh show dev "$IF" 2>/dev/null \
            | awk '/^fe80:/ {print $1; exit}')

        if [ -n "$NHLL" ]; then  
            break
        fi
        remaining_delay=$(awk -v d="$PROBE_SLEEP_SEC" -v s="$attempt_start_ts" -v e="$ping_done_ts" \
            'BEGIN { r = d - (e - s); if (r > 0) printf "%.6f", r; else print "0"; }')
        if [ "$(awk -v r="$remaining_delay" 'BEGIN { if (r > 0) print 1; else print 0; }')" -eq 1 ]; then
            sleep "$remaining_delay"
        fi
    done
fi

if [ -z "$NHLL" ]; then
    echo "No link-local neighbor discovered on $IF, cannot add route to $DST"
    exit 1
fi

ip -6 route replace "$DST" via "$NHLL" dev "$IF" metric "$MET"
