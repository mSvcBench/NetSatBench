#!/bin/bash
#
# ============================================================
# Usage:
#   ./create-sat-bridge.sh <SAT_HOST_CIDR> [SAT_HOST] [SSH_USERNAME]
#
# Examples:
#   ./create-sat-bridge.sh 172.100.0.0/16 host-1 ubuntu
#   ./create-sat-bridge.sh 172.101.0.0/16 10.0.1.144 ubuntu
#   ./create-sat-bridge.sh 172.102.0.0/16 host-3
#
# Description:
#   - Creates or verifies the Docker network 'sat-bridge' on the target host.
#   - Auto-detects correct CIDR based on the host name/IP if missing or mismatched.
#   - Configures static inter-host routes (so sat containers can reach each other).
# ============================================================

set -euo pipefail

# Input arguments
SAT_HOST_CIDR="${1:-}"           # Optional; auto-assigned if omitted
SAT_HOST="${2:-127.0.0.1}"       # Default = localhost
SSH_USERNAME="${3:-$(whoami)}"   # Default = current user
SAT_HOST_BRIDGE_NAME="sat-bridge"

# === Determine correct CIDR based on hostname/IP ===
case "$SAT_HOST" in
  *host-1*|10.0.1.215)
    AUTO_CIDR="172.100.0.0/16"
    ;;
  *host-2*|10.0.1.144)
    AUTO_CIDR="172.101.0.0/16"
    ;;
  *host-3*|10.0.2.199)
    AUTO_CIDR="172.102.0.0/16"
    ;;
  *)
    echo "⚠️  Unknown host ($SAT_HOST). Defaulting CIDR → 172.200.0.0/16"
    AUTO_CIDR="172.200.0.0/16"
    ;;
esac

# Use automatic CIDR if none provided or mismatched
if [[ -z "$SAT_HOST_CIDR" || "$SAT_HOST_CIDR" != "$AUTO_CIDR" ]]; then
  SAT_HOST_CIDR="$AUTO_CIDR"
fi

echo "🌍 Target host: $SAT_HOST  →  Subnet: $SAT_HOST_CIDR"

# === Step 1: Create or verify Docker network remotely ===
if ssh "$SSH_USERNAME@$SAT_HOST" docker network inspect "$SAT_HOST_BRIDGE_NAME" >/dev/null 2>&1; then
  echo "✔️  Docker network '$SAT_HOST_BRIDGE_NAME' already exists on $SAT_HOST."
else
  echo "🧱 Creating Docker network '$SAT_HOST_BRIDGE_NAME' on $SAT_HOST ..."
  ssh "$SSH_USERNAME@$SAT_HOST" docker network create \
    --driver=bridge \
    --subnet="$SAT_HOST_CIDR" \
    -o com.docker.network.bridge.enable_ip_masquerade=false \
    "$SAT_HOST_BRIDGE_NAME"
  echo "✅ Docker network '$SAT_HOST_BRIDGE_NAME' created successfully on $SAT_HOST."
fi

# === Step 2: Add static routes between hosts ===
echo "📡 Configuring inter-host routes on $SAT_HOST ..."

ssh "$SSH_USERNAME@$SAT_HOST" bash -s <<'EOSSH'
set -e

# IP addresses of hosts
H1=10.0.1.215   # host-1
H2=10.0.1.144   # host-2
H3=10.0.2.199   # host-3

# Detect local IP
LOCAL_IP=$(hostname -I | awk '{print $1}')

if [[ "$LOCAL_IP" == "$H1" ]]; then
  sudo ip route replace 172.101.0.0/16 via $H2 dev ens3 || true
  sudo ip route replace 172.102.0.0/16 via $H3 dev ens3 || true
  echo "📍 host-1 routes applied."
elif [[ "$LOCAL_IP" == "$H2" ]]; then
  sudo ip route replace 172.100.0.0/16 via $H1 dev ens3 || true
  sudo ip route replace 172.102.0.0/16 via $H3 dev ens3 || true
  echo "📍 host-2 routes applied."
elif [[ "$LOCAL_IP" == "$H3" ]]; then
  sudo ip route replace 172.100.0.0/16 via $H1 dev ens3 || true
  sudo ip route replace 172.101.0.0/16 via $H2 dev ens3 || true
  echo "📍 host-3 routes applied."
else
  echo "⚠️  Unknown host ($LOCAL_IP), skipping route configuration."
fi
EOSSH

echo "✅ Network and route setup complete for $SAT_HOST"
