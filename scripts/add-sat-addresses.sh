#!/usr/bin/env bash
# add-sat-addresses.sh — assign /32 IPs to br1..brN inside a satellite container
# Usage:
#   ./add-sat-addresses.sh <SAT_NAME> <N_ANTENNAS> <SAT_NET_CIDR> [SAT_HOST] [SSH_USERNAME]

set -euo pipefail

SAT_NAME="${1:-}"
N_ANTENNAS="${2:-}"
SAT_NET_CIDR="${3:-}"
SAT_HOST="${4:-127.0.0.1}"
SSH_USERNAME="${5:-$(whoami)}"

if [[ -z "$SAT_NAME" || -z "$N_ANTENNAS" || -z "$SAT_NET_CIDR" ]]; then
  echo "Usage: $0 <SAT_NAME> <N_ANTENNAS> <SAT_NET_CIDR> [SAT_HOST] [SSH_USERNAME]" >&2
  exit 1
fi

IP_BASE="$(echo "$SAT_NET_CIDR" | cut -d'.' -f1-3)"
SUBNET="$(echo "$SAT_NET_CIDR" | cut -d'/' -f2)"

#LIMIT_MBPS=2
#BURST_KBIT=32
#LATENCY_MS=400

#apply_tc_limit() {
#  local iface="$1"
#  ssh "$SSH_USERNAME@$SAT_HOST" docker exec "$SAT_NAME" tc qdisc replace dev "$iface" root tbf rate "${LIMIT_MBPS}mbit" burst "${BURST_KBIT}kbit" latency "${LATENCY_MS}ms"
#  echo "⏳ Applied TC limit on $iface (rate=${LIMIT_MBPS}mbit)"
#}

# Do all work with a single SSH + single docker exec to avoid runtime races and reduce noise.
ssh "$SSH_USERNAME@$SAT_HOST" "docker exec \"$SAT_NAME\" bash -lc '
  set -euo pipefail
  IP_BASE=\"$IP_BASE\"
  N=\"$N_ANTENNAS\"
  assigned=0

  command -v ip >/dev/null 2>&1

  for i in \$(seq 1 \"\$N\"); do
    BR=\"br\$i\"
    # Skip silently if bridge does not exist
    ip link show \"\$BR\" >/dev/null 2>&1 || continue

    # Add /32 only if not present
    if ! ip addr show \"\$BR\" | grep -q \"\\b\$IP_BASE.\$i\\b\"; then
      ip addr add \"\$IP_BASE.\$i/32\" dev \"\$BR\"
      ip link set \"\$BR\" up
      assigned=\$((assigned+1))
    fi
  done

  echo \"done: \$assigned assigned in $SAT_NAME\"
'"
