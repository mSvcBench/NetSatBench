#!/bin/bash
# Usage: ./link-actions.sh ACTION EP1 ANT1 IP1 EP2 ANT2 IP2 VNI [BW] [BURST] [LATENCY]

set -euo pipefail

: "${SAT_NAME:?SAT_NAME env required}"

# 1. READ ARGUMENTS
ACTION="$1"  # "add", "update", "del"
EP1="$2"; ANT1="$3"; IP1="$4"
EP2="$5"; ANT2="$6"; IP2="$7"
TARGET_VNI="$8"
TARGET_BW="${9:-}"
TARGET_BURST="${10:-}"
TARGET_LATENCY="${11:-}"

# 2. IDENTIFY SIDES
if [[ "$SAT_NAME" == "$EP1" ]]; then
    LOCAL_ANT="$ANT1"; REMOTE_SAT="$EP2"; REMOTE_ANT="$ANT2"; REMOTE_IP="$IP2"
elif [[ "$SAT_NAME" == "$EP2" ]]; then
    LOCAL_ANT="$ANT2"; REMOTE_SAT="$EP1"; REMOTE_ANT="$ANT1"; REMOTE_IP="$IP1"
else
    exit 0
fi

VXLAN_IF="${REMOTE_SAT}_a${REMOTE_ANT}"
TARGET_BRIDGE="br${LOCAL_ANT}"
LOCAL_IP=$(ip -4 addr show eth0 | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -n1)

# Helper: Apply TC safely (Idempotent)
apply_tc() {
    if [[ -n "$TARGET_BW" && -n "$TARGET_BURST" && -n "$TARGET_LATENCY" ]]; then
        if ip link show "$VXLAN_IF" >/dev/null 2>&1; then
            # Atomic replace (Update). If fails, Add.
            if ! tc qdisc replace dev "$VXLAN_IF" root tbf rate "$TARGET_BW" burst "$TARGET_BURST" latency "$TARGET_LATENCY" 2>/dev/null; then
                tc qdisc add dev "$VXLAN_IF" root tbf rate "$TARGET_BW" burst "$TARGET_BURST" latency "$TARGET_LATENCY" 2>/dev/null || true
            fi
            echo "   🚦 TC Set: $TARGET_BW , $TARGET_BURST , $TARGET_LATENCY   "
        fi
    fi
}

# ==============================================================================
#  🗑️ ACTION: DELETE
# ==============================================================================
if [[ "$ACTION" == "del" ]]; then
    if ip link show "$VXLAN_IF" >/dev/null 2>&1; then
        echo "🗑️  Deleting Link: $VXLAN_IF"
        ip link del "$VXLAN_IF"
    fi
    echo "    ✅ Link Deleted: $VXLAN_IF"

    exit 0
fi

# ==============================================================================
#  ➕ ACTION: ADD
# ==============================================================================
if [[ "$ACTION" == "add" ]]; then
    if ip link show "$VXLAN_IF" >/dev/null 2>&1; then
        # Exists? Treat as update (Apply TC and exit)
        ## apply tc only if TARGET_BW, TARGET_BURST and TARGET_LATENCY are set
        if [[ -n "$TARGET_BW" && -n "$TARGET_BURST" && -n "$TARGET_LATENCY" ]]; then
            apply_tc
        fi
        exit 0
    fi

    echo "➕ Creating Link: $VXLAN_IF (VNI: $TARGET_VNI)"
    ip link add "$VXLAN_IF" type vxlan id "$TARGET_VNI" remote "$REMOTE_IP" local "$LOCAL_IP" dev eth0 dstport 4789 2>/dev/null || true
    ip link set "$VXLAN_IF" mtu 1350
    ip link add "$TARGET_BRIDGE" type bridge 2>/dev/null || true
    ip link set "$TARGET_BRIDGE" up 2>/dev/null || true
    ip link set "$VXLAN_IF" master "$TARGET_BRIDGE"
    ip link set dev "$VXLAN_IF" up
    
    apply_tc
    echo "   ✅ Link Created: $VXLAN_IF"

    exit 0
fi