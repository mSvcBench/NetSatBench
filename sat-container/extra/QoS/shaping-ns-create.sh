#!/bin/bash

# This script redirects locally generated and forwarded traffic through a dedicated
# “shape” network namespace before reinjecting it into the root namespace.
#
# The indirection allows the application of fine-grained traffic shaping policies
# (e.g., tc qdisc, filters, classful scheduling) on veth0_rt without modifying the
# primary per link policy applied to the vl_x_y interface representing satellite-to-x links.
#
# It can be invoked from the “run” section of an epoch file via:
#     /app/extra/QoS/shaping-ns-create.sh
# when shaping is required, and later removed using:
#     /app/extra/QoS/shaping-ns-delete.sh
#
# Logical packet path:
#   input_link
#        ↓
#      vl_x_y
#        ↓
#     veth0_rt
#        ↓
#   [ veth0_ns → shape namespace → veth1_ns ]
#        ↓
#     veth1_rt
#        ↓
#      vl_y_x
#        ↓
#    output_link
#
# In this architecture, the shape namespace acts as a controlled processing
# domain where traffic can be delayed, rate-limited, reordered, or otherwise
# manipulated before returning to the main routing context.

set -euo pipefail

# Avoid re-applying shaping namespace setup if it already exists.
if ip link show veth0_rt >/dev/null 2>&1; then
  echo "Warning: interface veth0_rt already exists, skipping shaping namespace setup."
  exit 0
fi

# ---------- veth pairs ----------
echo "Creating veth pairs..."
ip link add veth0_rt type veth peer name veth0_ns
ip link add veth1_rt type veth peer name veth1_ns

# ---------- IPv4 addressing (use link-local) ----------
echo "Configuring IPv4 addresses..."
ip address add 169.254.0.0/31 dev veth0_rt
ip address add 169.254.0.3/31 dev veth1_rt
ip link set veth0_rt up
ip link set veth1_rt up

# ---------- namespace ----------
echo "Creating shape namespace..."
ip netns add shape
ip link set veth0_ns netns shape
ip link set veth1_ns netns shape
ip netns exec shape ip link set veth0_ns up
ip netns exec shape ip link set veth1_ns up
ip netns exec shape ip address add 169.254.0.1/31 dev veth0_ns
ip netns exec shape ip address add 169.254.0.2/31 dev veth1_ns

# enable IPv4 forwarding in shape
ip netns exec shape sysctl -w net.ipv4.ip_forward=1 > /dev/null #redirect to stderr to avoid polluting output

# default route in shape back to root via veth1 link
ip netns exec shape ip route add default via 169.254.0.3

# ---------- mangle rules (IPv4) ----------
echo "Configuring mangle rules and policy routing..."

# do not redirect packets returning from shape
iptables -t mangle -A PREROUTING -i veth1_rt -j ACCEPT

# mark only local IPv4 traffic routed to VXLAN interfaces (overlay inner traffic)
iptables -t mangle -A OUTPUT -o vl+ -j MARK --set-mark 0x01/0xFF

# mark only incoming IPv4 traffic from VXLAN interfaces (overlay inner traffic)
iptables -t mangle -A PREROUTING -i vl+ -j MARK --set-mark 0x01/0xFF

ip rule add fwmark 0x01/0xFF table 100
ip route add default via 169.254.0.1 table 100


sysctl -w net.ipv4.conf.veth1_rt.accept_local=1 > /dev/null 
sysctl -w net.ipv4.conf.veth1_rt.rp_filter=0 > /dev/null 
sysctl -w net.ipv4.conf.all.rp_filter=0 > /dev/null 

echo "Shaping namespace created and configured successfully."
