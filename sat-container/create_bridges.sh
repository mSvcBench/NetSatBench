#!/usr/bin/env bash
set -euo pipefail

echo " 🚀 Starting SAT internal agent for ${SAT_NAME:-<unknown>}"

# ======================
# 1️⃣ Fix Environment
# ======================
ulimit -n 1024

# ======================
# 2️⃣ SSH setup
# ======================
echo " 🔑 Setting up SSH daemon..."
mkdir -p /var/run/sshd
chmod 700 /root/.ssh
touch /root/.ssh/known_hosts
chmod 644 /root/.ssh/known_hosts

# Start SSHD in background
nohup /usr/sbin/sshd > /var/log/sshd.log 2>&1 &

if [[ -n "${REMOTE_SCRIPT_HOST:-}" ]]; then
    host="${REMOTE_SCRIPT_HOST%%:*}"
    echo "    Pre-trusting SSH host key for $host ..."
    ssh-keyscan -H "$host" >> /root/.ssh/known_hosts 2>/dev/null || true
fi

# ======================
# 3️⃣ Wait for Etcd
# ======================
export ETCD_HOST="${ETCD_ENDPOINT%%:*}"
export ETCD_PORT="${ETCD_ENDPOINT##*:}"

WAIT_RETRIES="${ETCD_WAIT_RETRIES:-30}"
WAIT_DELAY="${ETCD_WAIT_DELAY_SEC:-2}"

echo " ⏳ Waiting for etcd $ETCD_HOST:$ETCD_PORT ..."
for i in $(seq 1 "$WAIT_RETRIES"); do
    if timeout 2 bash -lc "echo > /dev/tcp/$ETCD_HOST/$ETCD_PORT" 2>/dev/null; then
        echo "    ✅ Etcd reachable."
        break
    fi
    echo "    ... retry $i/$WAIT_RETRIES"
    sleep "$WAIT_DELAY"
done

# ======================
# 4️⃣ Determine Antenna Count & Create Bridges
# ======================
echo " 🔍 Determining configuration for $SAT_NAME..."

# Simple logic based on naming convention:
# sat* => 5 antennas
# ground* => 1 antenna
if [[ "$SAT_NAME" == sat* ]]; then
    echo "    🛰️  Node is a Satellite. Setting 5 antennas."
    N_ANTENNAS=5
elif [[ "$SAT_NAME" == ground* ]]; then
    echo "    🌍 Node is a Ground Station. Setting 1 antenna."
    N_ANTENNAS=1
else
    echo "    Unknown node type '$SAT_NAME'. Defaulting to 1 antenna."
    N_ANTENNAS=1
fi

echo " 🌉 Creating $N_ANTENNAS internal bridges..."

create_bridge() {
    local BR="$1"
    if ip link show "$BR" >/dev/null 2>&1; then
        echo "    Bridge $BR already exists, skipping."
        return
    fi
    ip link add name "$BR" type bridge
    ip link set dev "$BR" up
    echo "    ✅ Created $BR"
}

for i in $(seq 1 "$N_ANTENNAS"); do
    create_bridge "br$i"
done
echo "    All bridges (br1–br$N_ANTENNAS) are ready."

# ======================
# 5️⃣ Fix SSH key permissions
# ======================
if [[ -f /root/.ssh/id_rsa ]]; then
    chmod 600 /root/.ssh/id_rsa 2>/dev/null || \
        echo "    ⚠️  Cannot chmod /root/.ssh/id_rsa (maybe read-only volume)"
fi

# ======================
# 6️⃣ Launch the internal agent
# ======================
echo " 🚀 Launching sat-agent-internal.py ..."
exec python3 -u /agent/sat-agent-internal.py