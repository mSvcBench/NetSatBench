#!/bin/bash

# Usage: ./delete-sat-bridge.sh [SAT_HOST] [SSH_USERNAME]
# Example:
#   ./delete-sat-bridge.sh
#   ./delete-sat-bridge.sh 192.168.1.10 alice

# Enable command echoing (optional)
#set -x

# Input arguments
SAT_HOST="${1:-127.0.0.1}"       # Default to localhost if not provided
SSH_USERNAME="${2:-$(whoami)}"   # Default to local user
SAT_HOST_BRIDGE_NAME="sat-bridge"

# Check if the Docker network exists on the remote host
if ssh "$SSH_USERNAME@$SAT_HOST" docker network inspect "$SAT_HOST_BRIDGE_NAME" >/dev/null 2>&1; then
  echo "Deleting Docker network '$SAT_HOST_BRIDGE_NAME' on $SAT_HOST..."
  ssh "$SSH_USERNAME@$SAT_HOST" docker network rm "$SAT_HOST_BRIDGE_NAME"
  echo "✅ Docker network '$SAT_HOST_BRIDGE_NAME' deleted from $SAT_HOST."
else
  echo "Docker network '$SAT_HOST_BRIDGE_NAME' does not exist on $SAT_HOST. Nothing to delete."
fi
