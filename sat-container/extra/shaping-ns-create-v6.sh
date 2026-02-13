#!/bin/bash

# This script redirects locally generated and forwarded traffic through a dedicated
# “shape” network namespace before reinjecting it into the root namespace.
#
# The indirection allows the application of fine-grained traffic shaping policies
# (e.g., tc qdisc, filters, classful scheduling) on veth0_rt without modifying the
# primary per link policy applied to the vl_x_y interface representing satellite-to-x links.
#
# It can be invoked from the “run” section of an epoch file via:
#     /app/extra/shaping-ns-create-v6.sh
# when shaping is required, and later removed using:
#     /app/extra/shaping-ns-delete-v6.sh
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

# ---------- veth pairs ----------
echo "Creating veth pairs..."
ip link add veth0_rt type veth peer name veth0_ns
ip link add veth1_rt type veth peer name veth1_ns

# ---------- IPv6 addressing (use ULA) ----------
# link0: fd00:0:0:1::/127  (rt ::0  <-> ns ::1)
# link1: fd00:0:0:2::/127  (ns ::0  <-> rt ::1)
echo "Configuring IPv6 addresses..."
ip -6 addr add fd00:0:0:1::0/127 dev veth0_rt
ip -6 addr add fd00:0:0:2::1/127 dev veth1_rt

ip link set veth0_rt up
ip link set veth1_rt up

# ---------- namespace ----------
echo "Creating shape namespace..."
ip netns add shape
ip link set veth0_ns netns shape
ip link set veth1_ns netns shape

ip netns exec shape ip link set lo up
ip netns exec shape ip link set veth0_ns up
ip netns exec shape ip link set veth1_ns up

ip netns exec shape ip -6 addr add fd00:0:0:1::1/127 dev veth0_ns
ip netns exec shape ip -6 addr add fd00:0:0:2::0/127 dev veth1_ns

# enable IPv6 forwarding in shape
ip netns exec shape sysctl -w net.ipv6.conf.all.forwarding=1

# default route in shape back to root via veth1 link
ip netns exec shape ip -6 route add default via fd00:0:0:2::1 dev veth1_ns > /dev/null 
# ---------- mangle rules (IPv6) ----------
echo "Configuring mangle rules and policy routing..."
# do not redirect VXLAN processing (still UDP/4789)
ip6tables -t mangle -A PREROUTING -p udp --dport 4789 -j ACCEPT
ip6tables -t mangle -A OUTPUT     -p udp --dport 4789 -j ACCEPT

# do not redirect packets returning from shape
ip6tables -t mangle -A PREROUTING -i veth1_rt -j ACCEPT

# mark all local-originated IPv6 packets those not already accepted (e.g., UDP/4789)
ip6tables -t mangle -A OUTPUT -j MARK --set-mark 0x01/0xFF

# mark all incoming IPv6 packets except those previusly accepted (e.g., from shape or UDP/4789)
ip6tables -t mangle -A PREROUTING -j MARK --set-mark 0x01/0xFF

# ---------- policy routing ----------
ip -6 rule add fwmark 0x01/0xFF table 100
ip -6 route add default via fd00:0:0:1::1 dev veth0_rt table 100

# ---------- sysctls (IPv6 equivalents / safe defaults) ----------
# reverse-path filtering is IPv4-only; accept_local is IPv4-only.
# For IPv6, disable RA acceptance on these veths to keep routes deterministic.
sysctl -w net.ipv6.conf.veth0_rt.accept_ra=0 > /dev/null 
sysctl -w net.ipv6.conf.veth1_rt.accept_ra=0 > /dev/null 
sysctl -w net.ipv6.conf.all.accept_ra=0 > /dev/null 

echo "Shaping namespace created and configured successfully."

