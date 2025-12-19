#!/usr/bin/env bash
set -euo pipefail

echo " 🚀 Starting SAT internal agent for ${SAT_NAME:-<unknown>}"

# ======================
# Fix Environment
# ======================
ulimit -n 1024

# ======================
# Wait for Etcd
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
# Launch the internal agent
# ======================
echo " 🚀 Launching sat-agent.py ..."
/usr/bin/screen -S APP -s /bin/bash -t win0 -A -d -m
screen -S APP -p win0 -X stuff $'python3 -u /app/sat-agent.py \n'
sleep infinity