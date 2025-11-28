#!/bin/bash

# add-link.sh — add VXLAN link between two satellite containers
# Usage:
# ./add-link.sh <SRC_SAT> <SRC_ANTENNA> [SRC_SAT_HOST] <DST_SAT> <DST_ANTENNA> [DST_SAT_HOST] [SSH_USERNAME]

SRC_SAT="$1"
SRC_ANTENNA="$2"
SRC_SAT_HOST="${3:-127.0.0.1}"
DST_SAT="$4"
DST_ANTENNA="$5"
DST_SAT_HOST="${6:-127.0.0.1}"
SSH_USERNAME="${7:-$(whoami)}"

if [ -z "$SRC_SAT" ] || [ -z "$SRC_ANTENNA" ] || [ -z "$DST_SAT" ] || [ -z "$DST_ANTENNA" ]; then
  echo "Usage: $0 <SRC_SAT> <SRC_ANTENNA> [SRC_SAT_HOST] <DST_SAT> <DST_ANTENNA> [DST_SAT_HOST] [SSH_USERNAME]"
  exit 1
fi

vxlan_if_in_SRC="${DST_SAT}_a${DST_ANTENNA}"
vxlan_if_in_DST="${SRC_SAT}_a${SRC_ANTENNA}"

# Unique VNI calculation
vni_input="${SRC_SAT}_${SRC_ANTENNA}_${DST_SAT}_${DST_ANTENNA}"
vxlan_vni=$(echo -n "$vni_input" | cksum | awk '{print $1 % 16777215 + 1}')

# ---------------------------------------------------
# Fetch eth0 IPs from running containers
# ---------------------------------------------------
echo "🔍 Fetching IPs..."
SRC_SAT_IP=$(ssh "$SSH_USERNAME@$SRC_SAT_HOST" "docker exec $SRC_SAT ip -4 addr show eth0 | grep -oP '(?<=inet\\s)\\d+(\\.\\d+){3}'")
DST_SAT_IP=$(ssh "$SSH_USERNAME@$DST_SAT_HOST" "docker exec $DST_SAT ip -4 addr show eth0 | grep -oP '(?<=inet\\s)\\d+(\\.\\d+){3}'")

if [ -z "$SRC_SAT_IP" ] || [ -z "$DST_SAT_IP" ]; then
    echo "❌ Error: Could not fetch container IPs. Check SSH connection and container names."
    exit 1
fi

# ---------------------------------------------------
# Update /config/satellites/<sat> JSON in etcd: set eth0_ip
# ---------------------------------------------------
update_etcd_eth0() {
  local SAT_NAME="$1"
  local ETH0_IP="$2"

  local existing
  existing=$(etcdctl get "/config/satellites/$SAT_NAME" --print-value-only 2>/dev/null)

  if [[ -z "$existing" || "$existing" =~ ^[[:space:]]*$ ]]; then
    return
  fi

  local new_value
  new_value=$(
    EXISTING_JSON="$existing" ETH0_IP="$ETH0_IP" python3 << 'PY'
import json, os, sys
ip = os.environ["ETH0_IP"]
raw = os.environ.get("EXISTING_JSON", "")
try:
    data = json.loads(raw)
    data["eth0_ip"] = ip
    print(json.dumps(data))
except:
    sys.exit(1)
PY
  )

  if [ $? -eq 0 ] && [ -n "$new_value" ]; then
    printf '%s' "$new_value" | etcdctl put "/config/satellites/$SAT_NAME" >/dev/null
  fi
}

update_etcd_eth0 "$SRC_SAT" "$SRC_SAT_IP"
update_etcd_eth0 "$DST_SAT" "$DST_SAT_IP"

# ---------------------------------------------------
# VXLAN setup (FIXED: Uses 'remote' and removes TC)
# ---------------------------------------------------
setup_vxlan() {
  local HOST="$1"
  local SSH_USER="$2"
  local CONTAINER="$3"
  local REMOTE_IP="$4"
  local BRIDGE="br$5"
  local VXLAN_IF="$6"

  echo "⚙️  Configuring $CONTAINER on $HOST..."
  
  ssh "$SSH_USER@$HOST" bash -c "'
    # 1. Clean up old interface if exists
    if docker exec \"$CONTAINER\" ip link show \"$VXLAN_IF\" > /dev/null 2>&1; then
        docker exec \"$CONTAINER\" ip link del \"$VXLAN_IF\"
    fi

    # 2. Create VXLAN interface using DIRECT REMOTE IP (Fixes the connectivity issue)
    docker exec \"$CONTAINER\" ip link add \"$VXLAN_IF\" type vxlan \
        id \"$vxlan_vni\" \
        remote \"$REMOTE_IP\" \
        dev eth0 \
        dstport 4789

    # 3. Attach to bridge and bring up
    docker exec \"$CONTAINER\" ip link set \"$VXLAN_IF\" master \"$BRIDGE\"
    docker exec \"$CONTAINER\" ip link set dev \"$VXLAN_IF\" up
  '"
}

# Run Setup for both sides
setup_vxlan "$SRC_SAT_HOST" "$SSH_USERNAME" "$SRC_SAT" "$DST_SAT_IP" "$SRC_ANTENNA" "$vxlan_if_in_SRC"
setup_vxlan "$DST_SAT_HOST" "$SSH_USERNAME" "$DST_SAT" "$SRC_SAT_IP" "$DST_ANTENNA" "$vxlan_if_in_DST"

echo "========================================"
echo "✅ Link Established: $SRC_SAT <--> $DST_SAT"
echo "   VNI: $vxlan_vni"
echo "   SRC: $SRC_SAT ($SRC_SAT_IP) -> $vxlan_if_in_SRC (Remote: $DST_SAT_IP)"
echo "   DST: $DST_SAT ($DST_SAT_IP) -> $vxlan_if_in_DST (Remote: $SRC_SAT_IP)"
echo "========================================"