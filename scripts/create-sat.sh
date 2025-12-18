#!/bin/bash
# create-sat.sh — create a satellite container
# Usage: ./create-sat.sh <NAME> <N_ANT> <HOST> <USER> <IMAGE>

set -euo pipefail

SAT_NAME="${1:-}"
n_antennas="${2:-}"
SAT_HOST="${3:-host-1}"
SSH_USERNAME="${4:-$(whoami)}"
CONTAINER_IMAGE="${5:-shahramdd/sat:7.6}"

HOST1_ALIAS="host-1"
HOST1_IP="10.0.1.215"
SAT_HOST_BRIDGE_NAME="sat-bridge"

if [[ -z "$SAT_NAME" || -z "$n_antennas" ]]; then
  echo "Usage: $0 <SAT_NAME> <n_antennas> [SAT_HOST] [SSH_USERNAME] [CONTAINER_IMAGE]"
  exit 1
fi

# Ensure permissions on remote host
ssh "$SSH_USERNAME@$SAT_HOST" chmod 600 /home/ubuntu/.ssh/id_rsa >/dev/null 2>&1 || true

# Create Docker Network if missing
ssh "$SSH_USERNAME@$SAT_HOST" "docker network create $SAT_HOST_BRIDGE_NAME 2>/dev/null || true"

# Remove existing container
if ssh "$SSH_USERNAME@$SAT_HOST" docker ps -a --format '{{.Names}}' | grep -Fxq "$SAT_NAME"; then
  echo "Container '$SAT_NAME' already exists on $SAT_HOST. Recreating..."
  ssh "$SSH_USERNAME@$SAT_HOST" docker rm -f "$SAT_NAME" >/dev/null 2>&1 || true
fi

# Run the container
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
  -e UPDATE_LINK_SH="/agent/update-link.sh" \
  -v /home/ubuntu/.ssh/id_rsa:/root/.ssh/id_rsa:ro \
  -v /home/ubuntu/.ssh/id_rsa.pub:/root/.ssh/id_rsa.pub:ro \
  "$CONTAINER_IMAGE"

echo "✅ Satellite '$SAT_NAME' created on $SAT_HOST."