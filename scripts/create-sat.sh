#!/bin/bash
# create-sat.sh — create a satellite container on any host, but always communicate with host-1
# Usage:
#   ./create-sat.sh <SAT_NAME> <N_ANTENNAS> [SAT_HOST] [SSH_USERNAME]
# Example:
#   ./create-sat.sh sat5 5 host-2 ubuntu

set -euo pipefail

SAT_NAME="${1:-}"
N_ANTENNAS="${2:-}"
SAT_HOST="${3:-host-1}"         # Host where the container will be created (host-1 / host-2 / host-3)
SSH_USERNAME="${4:-$(whoami)}"

SAT_HOST_BRIDGE_NAME="sat-bridge"

# ➊ The target for coordination is always host-1
HOST1_ALIAS="host-1"
HOST1_IP="10.0.1.215"

if [[ -z "$SAT_NAME" || -z "$N_ANTENNAS" ]]; then
  echo "Usage: $0 <SAT_NAME> <N_ANTENNAS> [SAT_HOST] [SSH_USERNAME]"
  exit 1
fi

# Ensure the private key on the target host has correct permissions
ssh "$SSH_USERNAME@$SAT_HOST" chmod 600 /home/ubuntu/.ssh/id_rsa >/dev/null 2>&1 || true

# Create Docker network 'sat-bridge' if it doesn't exist
if ssh "$SSH_USERNAME@$SAT_HOST" docker network inspect "$SAT_HOST_BRIDGE_NAME" >/dev/null 2>&1; then
  echo "Docker network '$SAT_HOST_BRIDGE_NAME' already exists on $SAT_HOST."
else
  echo "Creating Docker network '$SAT_HOST_BRIDGE_NAME' on $SAT_HOST..."
  ssh "$SSH_USERNAME@$SAT_HOST" docker network create "$SAT_HOST_BRIDGE_NAME"
fi

# Remove existing container if it already exists
if ssh "$SSH_USERNAME@$SAT_HOST" docker ps -a --format '{{.Names}}' | grep -Fxq "$SAT_NAME"; then
  echo "Container '$SAT_NAME' already exists on $SAT_HOST. Recreating..."
  ssh "$SSH_USERNAME@$SAT_HOST" docker rm -f "$SAT_NAME" >/dev/null 2>&1 || true
fi

# Run the satellite container on SAT_HOST:
#  - Always includes --add-host for host-1
#  - Always sets REMOTE_SCRIPT_HOST=host-1 (agent communicates with host-1)
#  - SSH key from the same SAT_HOST is bind-mounted into the container
ssh "$SSH_USERNAME@$SAT_HOST" docker run -d \
  --name "$SAT_NAME" \
  --hostname "$SAT_NAME" \
  --net "$SAT_HOST_BRIDGE_NAME" \
  --privileged \
  --add-host "$HOST1_ALIAS:$HOST1_IP" \
  -e SAT_NAME="$SAT_NAME" \
  -e ETCD_ENDPOINT="$HOST1_IP:2379" \
  -e REMOTE_SCRIPT_HOST="$HOST1_ALIAS" \
  -e REMOTE_SCRIPT_USER="$SSH_USERNAME" \
  -e UPDATE_LINK_SH="/agent/update-link-internal.sh" \
  -v /home/ubuntu/.ssh/id_rsa:/root/.ssh/id_rsa:ro \
  -v /home/ubuntu/.ssh/id_rsa.pub:/root/.ssh/id_rsa.pub:ro \
  shahramdd/sat:7.6

echo "✅ Satellite '$SAT_NAME' created on $SAT_HOST (communicates with $HOST1_ALIAS=$HOST1_IP)."
