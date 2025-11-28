#!/bin/bash
# delete-link.sh — remove VXLAN link between two satellite containers
# Usage:
# ./del-link.sh <SRC_SAT> <SRC_ANTENNA> [SRC_SAT_HOST] <DST_SAT> <DST_ANTENNA> [DST_SAT_HOST] [SSH_USERNAME]

#set -x

# Input arguments
SRC_SAT="$1"
SRC_ANTENNA="$2"
SRC_SAT_HOST="${3:-127.0.0.1}"
DST_SAT="$4"
DST_ANTENNA="$5"
DST_SAT_HOST="${6:-127.0.0.1}"
SSH_USERNAME="${7:-$(whoami)}"

# Validate required arguments
if [ -z "$SRC_SAT" ] || [ -z "$SRC_ANTENNA" ] || [ -z "$DST_SAT" ] || [ -z "$DST_ANTENNA" ]; then
  echo "Usage: $0 <SRC_SAT> <SRC_ANTENNA> [SRC_SAT_HOST] <DST_SAT> <DST_ANTENNA> [DST_SAT_HOST] [SSH_USERNAME]"
  exit 1
fi

# Check if the container exists on the remote host
if ! ssh "$SSH_USERNAME@$SRC_SAT_HOST" docker ps -a --format '{{.Names}}' | grep -Fxq "$SRC_SAT"; then
  echo "❌ Container '$SRC_SAT' does not exist on host '$SRC_SAT_HOST'. Aborting."
  exit 1
fi
if ! ssh "$SSH_USERNAME@$DST_SAT_HOST" docker ps -a --format '{{.Names}}' | grep -Fxq "$DST_SAT"; then
  echo "❌ Container '$DST_SAT' does not exist on host '$DST_SAT_HOST'. Aborting."
  exit 1
fi

# Interface names (must match those used in creation)
vxlan_if_in_SRC="${DST_SAT}_a${DST_ANTENNA}"
vxlan_if_in_DST="${SRC_SAT}_a${SRC_ANTENNA}"

# Function to remove VXLAN interface from a container on a remote host
remove_vxlan_remote() {
  local HOST="$1"
  local SSH_USERNAME="$2"
  local CONTAINER="$3"
  local VXLAN_IF="$4"

  ssh "$SSH_USERNAME@$HOST" bash -c "'
    if docker exec \"$CONTAINER\" ip link show \"$VXLAN_IF\" > /dev/null 2>&1; then
      echo \"Removing VXLAN interface '$VXLAN_IF' from container '$CONTAINER' on $HOST\"
      docker exec \"$CONTAINER\" ip link del \"$VXLAN_IF\"
    else
      echo \"VXLAN interface '$VXLAN_IF' not found in container '$CONTAINER' on $HOST\"
    fi
  '"
}

# Remove VXLAN interfaces from both containers
remove_vxlan_remote "$SRC_SAT_HOST" "$SSH_USERNAME" "$SRC_SAT" "$vxlan_if_in_SRC"
remove_vxlan_remote "$DST_SAT_HOST" "$SSH_USERNAME" "$DST_SAT" "$vxlan_if_in_DST"

# Summary
echo "========================================"
echo "✅ Removed VXLAN interfaces:"
echo "- From $SRC_SAT on $SRC_SAT_HOST: $vxlan_if_in_SRC"
echo "- From $DST_SAT on $DST_SAT_HOST: $vxlan_if_in_DST"
echo "========================================"
