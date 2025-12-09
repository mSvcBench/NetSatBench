#!/bin/bash
set -euo pipefail
# Args: EP1 ANT1 IP1 EP2 ANT2 IP2
: "${SAT_NAME:?SAT_NAME env required}"

EP1="$1"; ANT1="$2"; IP1="$3"
EP2="$4"; ANT2="$5"; IP2="$6"

# Identity Check
if [[ "$SAT_NAME" == "$EP1" ]]; then
    LOCAL_ANT="$ANT1"; REMOTE_SAT="$EP2"; REMOTE_ANT="$ANT2"; REMOTE_IP="$IP2"
elif [[ "$SAT_NAME" == "$EP2" ]]; then
    LOCAL_ANT="$ANT2"; REMOTE_SAT="$EP1"; REMOTE_ANT="$ANT1"; REMOTE_IP="$IP1"
else
    exit 0
fi

# Calculate VNI (Consistent Hash)
VNI_INPUT="${EP1}_${ANT1}_${EP2}_${ANT2}"
TARGET_VNI=$(echo -n "$VNI_INPUT" | cksum | awk '{print $1 % 16777215 + 1}')

# Naming
VXLAN_IF="${REMOTE_SAT}_a${REMOTE_ANT}"
TARGET_BRIDGE="br${LOCAL_ANT}"
LOCAL_IP=$(ip -4 addr show eth0 | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -n1)

create_link() {
    ip link add "$VXLAN_IF" type vxlan id "$TARGET_VNI" remote "$REMOTE_IP" local "$LOCAL_IP" dev eth0 dstport 4789
    ip link set "$VXLAN_IF" mtu 1350
    # Ensure bridge exists (redundant safety)
    ip link add "$TARGET_BRIDGE" type bridge 2>/dev/null || true
    ip link set "$TARGET_BRIDGE" up 2>/dev/null || true
    ip link set "$VXLAN_IF" master "$TARGET_BRIDGE"
    ip link set dev "$VXLAN_IF" up
}

# Idempotency Check
if ip link show "$VXLAN_IF" >/dev/null 2>&1; then
    CUR_VNI=$(ip -d link show "$VXLAN_IF" | grep -oP 'vxlan id \K\d+')
    CUR_REM=$(ip -d link show "$VXLAN_IF" | grep -oP 'remote \K[\d.]+')
    CUR_MST=$(ip link show "$VXLAN_IF" | grep -oP 'master \K\S+' || echo "")

    if [[ "$CUR_VNI" != "$TARGET_VNI" || "$CUR_REM" != "$REMOTE_IP" || "$CUR_MST" != "$TARGET_BRIDGE" ]]; then
        echo "♻️ Recreating $VXLAN_IF (Changed)"
        ip link del "$VXLAN_IF" 2>/dev/null ; create_link
    fi
else
    echo "➕ Creating $VXLAN_IF"
    create_link
fi
