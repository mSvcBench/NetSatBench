#!/bin/bash

# Usage:
# ./remove_isis.sh <SRC_SAT> [SAT_HOST] [SSH_USERNAME]
# Example:
# ./remove_isis.sh sat3 host-2 ubuntu

# Required args
SRC_SAT="$1"
shift

# Optional args
SAT_HOST="${1:-127.0.0.1}"
SSH_USERNAME="${2:-$USER}"

if [[ -z "$SRC_SAT" ]]; then
  echo "Usage: $0 <SRC_SAT> [SAT_HOST] [SSH_USERNAME]"
  exit 1
fi

echo "Removing IS-IS configuration on container '$SRC_SAT' @ '$SAT_HOST'"
echo "→ Host: $SAT_HOST"
echo "→ SSH User: $SSH_USERNAME"
echo

# Run configuration removal remotely using SSH
ssh "$SSH_USERNAME@$SAT_HOST" bash -c "
  # Remove the IS-IS related configuration from frr.conf
  docker exec -i \"$SRC_SAT\" bash -c \"sed -i '/router isis/d' /etc/frr/frr.conf\"
  docker exec -i \"$SRC_SAT\" bash -c \"sed -i '/interface br/d' /etc/frr/frr.conf\"
  docker exec -i \"$SRC_SAT\" bash -c \"sed -i '/net 49.0001.0000.0000/d' /etc/frr/frr.conf\"
  docker exec -i \"$SRC_SAT\" bash -c \"sed -i '/isis circuit-type/d' /etc/frr/frr.conf\"
  docker exec -i \"$SRC_SAT\" bash -c \"sed -i '/ip router isis/d' /etc/frr/frr.conf\"

  # Remove the daemons configuration for ISIS
  docker exec -i \"$SRC_SAT\" bash -c \"sed -i '/isisd/d' /etc/frr/daemons\"

  # Restart frr service to apply changes
  docker exec \"$SRC_SAT\" bash -c \"service frr restart || systemctl restart frr\"
"

# Summary
echo
echo "========================================"
echo " IS-IS configuration has been removed from container '$SRC_SAT'."
echo "========================================"

