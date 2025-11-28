#!/bin/bash
# apply-tc.sh — Apply Traffic Control (Host Side)
# Usage: ./apply-tc.sh <SAT_NAME> <IFACE_NAME> <HOST> <USER> <BW> <BURST> <LATENCY>

SAT_NAME="$1"
IFACE_NAME="$2"
HOST="$3"
SSH_USER="$4"
BW="$5"
BURST="$6"
LATENCY="$7"

# Safety check: if no bandwidth provided, do nothing
if [[ -z "$BW" ]]; then
  echo "⚠️  No Bandwidth specified, skipping TC."
  exit 0
fi

# Execute TC command inside the container via SSH
# We use 'tc qdisc replace' so it works whether rules exist or not
ssh "$SSH_USER@$HOST" bash -c "'
  echo \"   ⚙️ Applying TC on $SAT_NAME ($IFACE_NAME): $BW $BURST $LATENCY\"
  docker exec \"$SAT_NAME\" tc qdisc replace dev \"$IFACE_NAME\" root tbf \
      rate \"$BW\" burst \"$BURST\" latency \"$LATENCY\"
'"