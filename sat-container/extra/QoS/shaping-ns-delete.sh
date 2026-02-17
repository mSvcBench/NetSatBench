#!/bin/bash

set -euo pipefail
# clean shaping namespace and links
echo "Cleaning up shaping namespace and related configuration..."
ip link delete veth0_rt
ip link delete veth1_rt
ip netns delete shape

# remove mangle rules (IPv4)
echo "Removing mangle rules and policy routing..."
iptables -t mangle -D PREROUTING -p udp --dport 4789 -j ACCEPT
iptables -t mangle -D OUTPUT     -p udp --dport 4789 -j ACCEPT
iptables -t mangle -D PREROUTING -i veth1_rt -j ACCEPT
iptables -t mangle -D OUTPUT -j MARK --set-mark 0x01/0xFF
iptables -t mangle -D PREROUTING -j MARK --set-mark 0x01/0xFF

# remove policy routing
ip rule del fwmark 0x01/0xFF table 100
echo "Shaping namespace and related configuration cleaned up successfully."