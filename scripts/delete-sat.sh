#!/bin/bash

# Usage:
# ./delete-sat.sh <SAT_NAME> [SAT_HOST] [SSH_USERNAME]
# Example:
#   ./delete-sat.sh sat1
#   ./delete-sat.sh sat1 192.168.1.10 myuser

#set -x

# Input arguments
SAT_NAME="$1"
SAT_HOST="${2:-127.0.0.1}"
SSH_USERNAME="${3:-$(whoami)}"

# Validate input
if [ -z "$SAT_NAME" ]; then
  echo "Usage: $0 <SAT_NAME> [SAT_HOST] [SSH_USERNAME]"
  exit 1
fi

# Check if the container exists on the remote host
if ! ssh "$SSH_USERNAME@$SAT_HOST" docker ps -a --format '{{.Names}}' | grep -Fxq "$SAT_NAME"; then
  echo "❌ Container '$SAT_NAME' does not exist on host '$SAT_HOST'. Aborting."
  exit 1
fi

# Check if the container is running and stop it
if ssh "$SSH_USERNAME@$SAT_HOST" docker ps -q -f name="^/${SAT_NAME}$" | grep -q .; then
  echo "Stopping container '$SAT_NAME' on $SAT_HOST..."
  ssh "$SSH_USERNAME@$SAT_HOST" docker stop "$SAT_NAME"
  echo "Removing container '$SAT_NAME' on $SAT_HOST..."
  ssh "$SSH_USERNAME@$SAT_HOST" docker rm "$SAT_NAME"
else
  echo "❌ Container '$SAT_NAME' is not running on $SAT_HOST or does not exist."
fi
