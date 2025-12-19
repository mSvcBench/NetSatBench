#!/bin/bash
# create-sat.sh — create a satellite container
# Usage: ./create-sat.sh <SAT_NAME> [SAT_HOST] [SSH_USERNAME] [SSH_KEY_PATH] [ETCD_HOST] [ETCD_PORT] [SAT_HOST_BRIDGE_NAME] [CONTAINER_IMAGE]

set -euo pipefail

SAT_NAME="${1:-}"
SAT_HOST="${2:-127.0.0.1}"
SSH_USERNAME="${3:-$(whoami)}"
SSH_KEY_PATH="${4:-$HOME/.ssh/id_rsa}"
ETCD_HOST="${5:-127.0.0.1}"
ETCD_PORT="${6:-2379}"
SAT_HOST_BRIDGE_NAME="${7:-sat-vnet}"
CONTAINER_IMAGE="${8:-msvcbench/sat-container:latest}"



if [ -z "$SAT_NAME" ]; then
  echo "Usage: $0 <SAT_NAME> [SAT_HOST] [CONTAINER_IMAGE]"
  exit 1
fi


# Remove existing container
if ssh -i $SSH_KEY_PATH "$SSH_USERNAME@$SAT_HOST" docker ps -a --format '{{.Names}}' | grep -Fxq "$SAT_NAME"; then
  echo "Container '$SAT_NAME' already exists on $SAT_HOST. Recreating..."
  ssh -i $SSH_KEY_PATH "$SSH_USERNAME@$SAT_HOST" docker rm -f "$SAT_NAME" >/dev/null 2>&1 || true
fi

# Run new sat-container
ssh -i $SSH_KEY_PATH "$SSH_USERNAME@$SAT_HOST" docker run -d \
  --name "$SAT_NAME" \
  --hostname "$SAT_NAME" \
  --net "$SAT_HOST_BRIDGE_NAME" \
  --privileged \
  -e SAT_NAME="$SAT_NAME" \
  -e ETCD_ENDPOINT="$ETCD_HOST:$ETCD_PORT" \
  "$CONTAINER_IMAGE"

echo "✅ Satellite '$SAT_NAME' created on $SAT_HOST."