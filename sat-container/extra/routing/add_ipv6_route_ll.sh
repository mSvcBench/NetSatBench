#!/usr/bin/env bash

# Usage:
#   ./add_ipv6_route_ll.sh <IF> <DST> <MET>
#
# Example:
#   ./add_ipv6_route_ll.sh vl_sat3_1 2001:db8:100::3/128 200

set -e

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

# echo "Discovering link-local neighbor on $IF ..."

NH=""

# Try up to 30 times (~3 seconds total)
for _ in $(seq 1 30); do
    # Trigger NDP activity (harmless multicast ping)
    ping -6 -c1 -w1 -I "$IF" ff02::1 >/dev/null 2>&1 || true

    # Try to extract first fe80:: neighbor
    NH=$(ip -6 neigh show dev "$IF" 2>/dev/null \
        | awk '/^fe80:/ {print $1; exit}')

    if [ -n "$NH" ]; then
        break
    fi

    sleep 0.1
done

if [ -z "$NH" ]; then
    echo "No link-local neighbor discovered on $IF"
    exit 1
fi

# echo "Using next-hop: $NH"

ip -6 route replace "$DST" via "$NH" dev "$IF" metric "$MET"

echo "Route installed:"
ip -6 route show "$DST"
