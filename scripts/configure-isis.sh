#!/bin/bash
# Usage:
# ./configure-isis.sh <SRC_SAT> <NET_ID> <ANTENNAS...> <SAT_NET_CIDR> [SAT_HOST] [SSH_USERNAME]

SRC_SAT="$1"

NET_ID="$2"

shift 2



ANTENNAS=()

while [[ "$1" =~ ^[0-9]+$ ]]; do

  ANTENNAS+=("$1")

  shift

done



SAT_NET="$1"

shift



SAT_HOST="host-0"

SSH_USERNAME="$USER"



if [[ $# -gt 0 ]]; then

  SAT_HOST="$1"

  shift

fi



if [[ $# -gt 0 ]]; then

  SSH_USERNAME="$1"

  shift

fi



# Validation

if [[ -z "$SRC_SAT" || -z "$NET_ID" || ${#ANTENNAS[@]} -eq 0 || -z "$SAT_NET" ]]; then

  echo "Usage: $0 <SRC_SAT> <NET_ID> <ANTENNAS...> <SAT_NET> [SAT_HOST] [SSH_USERNAME]"

  exit 1

fi



# host- Area ID extraction

if [[ "$SAT_HOST" =~ host-([0-9]+) ]]; then

  AREA_NUM="${BASH_REMATCH[1]}"

  AREA_ID=$(printf "%04d" "$AREA_NUM")

else

  AREA_ID="0000"

fi



# Extract subnet and loopback IP

CIDR_MASK="${SAT_NET##*/}"

BASE_IP="${SAT_NET%%/*}"

IFS='.' read -r o1 o2 o3 o4 <<< "$BASE_IP"

NET_PREFIX="$o1.$o2.$o3.0/$CIDR_MASK"

LO_IP="$o1.$o2.$o3.254/$CIDR_MASK"

LO_IFACE="lo"

ISIS_NAME="CORE"



# FRR

DAEMONS_CONF=$(cat <<EOF

zebra=yes

isisd=yes

staticd=yes



EOF

)



FRR_CONF=$(cat <<EOF

!

hostname $SRC_SAT

password zebra

enable password zebra

!

interface $LO_IFACE

 ip address $LO_IP

 ip router isis $ISIS_NAME

 isis circuit-type level-2

 isis passive-interface

!

router isis $ISIS_NAME

 net 49.$AREA_ID.0000.0000.$NET_ID.00

 is-type level-2

 metric-style wide

 log-adjacency-changes

 maximum-paths 8

 address-family ipv4 unicast

  maximum-paths 8

  redistribute static

 exit-address-family

!

EOF

)



# Add bridge interfaces with equal metrics

for antenna in "${ANTENNAS[@]}"; do

  br="br$antenna"

  FRR_CONF+=$'\n'"interface $br"

  FRR_CONF+=$'\n'" ip router isis $ISIS_NAME"

  FRR_CONF+=$'\n'" isis network point-to-point"

  FRR_CONF+=$'\n'" isis metric 2"

  FRR_CONF+=$'\n'"!"

done



# Add static route to enable redistribution

FRR_CONF+=$'\n'"ip route $NET_PREFIX Null0"$'\n'"!"

# Apply config to satellite

# WE FIX THE FD LIMIT AND REMOVE STALE LOCKS HERE

ssh -q "$SSH_USERNAME@$SAT_HOST" bash <<EOF > /dev/null 2>&1



# 1. Write daemons and config. 



docker exec -i "$SRC_SAT" tee /etc/frr/daemons > /dev/null <<EODAEMONS

$DAEMONS_CONF

EODAEMONS



docker exec -i "$SRC_SAT" tee /etc/frr/frr.conf > /dev/null <<EOFRR

$FRR_CONF

EOFRR



# 2. Ensure permissions and loopback

docker exec "$SRC_SAT" ip link set $LO_IFACE up || true

docker exec "$SRC_SAT" chown frr:frr /etc/frr/daemons /etc/frr/frr.conf

docker exec "$SRC_SAT" mkdir -p /var/run/frr

docker exec "$SRC_SAT" chown frr:frr /var/run/frr



# 3. CRITICAL FIXES FOR RESTART

docker exec "$SRC_SAT" bash -c "

    # Kill any existing FRR processes

    pkill watchfrr || true

    pkill zebra || true

    pkill isisd || true

    pkill staticd || true

    

    # REMOVE STALE LOCK FILES (Fixes 'watchfrr failed to start')

    rm -f /var/run/frr/*.pid

    rm -f /var/run/frr/*.socket

    

    # FIX FD LIMIT (Fixes 'FD Limit set: 1048576 is stupidly large')

    ulimit -n 1024

    

    # Start FRR using init script

    /etc/init.d/frr start

"

echo "✅ IS-IS configured and restarted on '$SRC_SAT' (Area $AREA_ID, Subnet $NET_PREFIX)"

EOF
