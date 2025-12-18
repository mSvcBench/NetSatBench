#!/bin/bash
# Usage: ./configure-isis.sh <NET_ID> <SYS_ID> <SAT_NET_CIDR> <ANTENNAS...>

NET_ID="$1"
SYS_ID="$2"
SAT_NET="$3"
shift 3

ANTENNAS=()
while [[ "$1" =~ ^[0-9]+$ ]]; do
    ANTENNAS+=("$1")
    shift
done

# Validation
if [[ -z "$NET_ID" || -z "$SYS_ID" || -z "$SAT_NET" ]]; then
    echo "Usage: $0 <NET_ID> <SYS_ID> <SAT_NET_CIDR> <ANTENNAS...>"
    echo "Got: $@"
    exit 1
fi

# Extract Area id from NET_ID (assuming NET_ID is the Area id part)
AREA_ID="$NET_ID"

# Extract subnet and loopback IP
CIDR_MASK="${SAT_NET##*/}"
BASE_IP="${SAT_NET%%/*}"
IFS='.' read -r o1 o2 o3 o4 <<< "$BASE_IP"
NET_PREFIX="$o1.$o2.$o3.0/$CIDR_MASK"
LO_IP="$o1.$o2.$o3.254/32"
LO_IFACE="lo"
ISIS_NAME="CORE"

echo "⚙️  Configuring FRR for Area $AREA_ID, SysID $SYS_ID, Subnet $NET_PREFIX"

# 1. Write Daemons Config
cat <<EOF > /etc/frr/daemons
zebra=yes
isisd=yes
staticd=yes
EOF

# 2. Write Main FRR Config
cat <<EOF > /etc/frr/frr.conf
!
hostname $(hostname)
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
 net 49.$AREA_ID.0000.0000.$SYS_ID.00
 is-type level-2
 metric-style wide
 log-adjacency-changes
 address-family ipv4 unicast
  maximum-paths 8
  redistribute static
 exit-address-family
!
EOF

# Add bridge interfaces
for antenna in "${ANTENNAS[@]}"; do
    br="br$antenna"
    echo "interface $br" >> /etc/frr/frr.conf
    echo " ip router isis $ISIS_NAME" >> /etc/frr/frr.conf
    echo " isis network point-to-point" >> /etc/frr/frr.conf
    echo " isis metric 10" >> /etc/frr/frr.conf
    echo "!" >> /etc/frr/frr.conf
done

# Add static route for redistribution
echo "ip route $NET_PREFIX Null0" >> /etc/frr/frr.conf
echo "!" >> /etc/frr/frr.conf

# 3. Apply Permissions
ip link set $LO_IFACE up || true
chown frr:frr /etc/frr/daemons /etc/frr/frr.conf
mkdir -p /var/run/frr
chown frr:frr /var/run/frr

# 4. Restart FRR (Local)
# Kill any existing FRR processes
pkill watchfrr || true
pkill zebra || true
pkill isisd || true
pkill staticd || true

# Remove stale lock files
rm -f /var/run/frr/*.pid
rm -f /var/run/frr/*.socket

# Fix FD Limit
ulimit -n 1024

# Start FRR
if [ -f /usr/lib/frr/frrinit.sh ]; then
    /usr/lib/frr/frrinit.sh start
else
    /etc/init.d/frr start
fi

echo "✅ IS-IS configured locally."