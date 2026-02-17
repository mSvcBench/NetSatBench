#!/bin/bash

# clean shaping namespace and related configuration

set -euo pipefail

# remove veth pairs (deleting root end removes the peer too)
echo "Cleaning up shaping namespace and related configuration..."
ip link del veth0_rt
ip link del veth1_rt

# remove namespace
ip netns del shape

# remove mangle rules (IPv6)
echo "Removing mangle rules and policy routing..."
ip6tables -t mangle -D PREROUTING -p udp --dport 4789 -j ACCEPT
ip6tables -t mangle -D OUTPUT     -p udp --dport 4789 -j ACCEPT
ip6tables -t mangle -D PREROUTING -i veth1_rt -j ACCEPT
ip6tables -t mangle -D OUTPUT -j MARK --set-mark 0x01/0xFF
ip6tables -t mangle -D PREROUTING -j MARK --set-mark 0x01/0xFF

# ---------- policy routing ----------
ip -6 rule del fwmark 0x01/0xFF table 100

echo "Shaping namespace and related configuration cleaned up successfully."
