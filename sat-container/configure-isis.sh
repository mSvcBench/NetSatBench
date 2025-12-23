#!/bin/bash
# Usage: ./configure-isis.sh <NET_ID> <SYS_ID> <subnet_ip> <ANTENNAS...>

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
    echo "Usage: $0 <NET_ID> <SYS_ID> <subnet_ip> <ANTENNAS...>"
    echo "Got: $@"
    exit 1
fi

# Extract Area id from NET_ID (assuming NET_ID is the Area id part)
AREA_ID="$NET_ID"

# Extract subnet and loopback IP from SAT_NET
CIDR_MASK="${SAT_NET##*/}"
BASE_IP="${SAT_NET%%/*}"
IFS='.' read -r o1 o2 o3 o4 <<< "$BASE_IP"
NET_PREFIX="$o1.$o2.$o3.$o4/$CIDR_MASK"
# Assign loopback IP as the last IP in the subnet
LO_IP_SUFFIX=$((2**(32 - CIDR_MASK) - 2))
LO_IP="$o1.$o2.$o3.$((o4 + LO_IP_SUFFIX))/$CIDR_MASK"

LO_IFACE="lo"
ISIS_NAME="CORE"

PART1="${SYS_ID:0:4}"
PART2="${SYS_ID:4:4}"

echo "⚙️  Configuring FRR for Area $AREA_ID, SysID $SYS_ID, Subnet $NET_PREFIX"

# 1. Write Daemons Config
cat <<EOF > /etc/frr/daemons
zebra=yes
isisd=yes
staticd=yes
# Pass ECMP limit to Zebra (Kernel route installation)
zebra_options=" -A 127.0.0.1 -e 64 --limit-fds 100000"
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
ip prefix-list PL-DENY-32 seq 10 permit 0.0.0.0/0 ge 32
!
route-map RM-ISIS-KERNEL deny 10
 match ip address prefix-list PL-DENY-32
exit
!
route-map RM-ISIS-KERNEL permit 100
exit
!
ip protocol isis route-map RM-ISIS-KERNEL
!
router isis $ISIS_NAME
 net $AREA_ID.0000.0000.$PART1.$PART2.00
 is-type level-2
 log-adjacency-changes
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