#!/usr/bin/env bash
set -euo pipefail

echo " 🚀 Starting SAT internal agent for ${NODE_NAME:-<unknown>}"

# ======================
# Fix Environment
# ======================
ulimit -n 1024

# ======================
# Enable IP forwarding and seg6
# ======================
sysctl -w net.ipv4.ip_forward=1
sysctl -w net.ipv6.conf.all.forwarding=1 
sysctl -w net.ipv6.conf.all.seg6_enabled=1
sysctl -w net.ipv6.conf.default.seg6_enabled=1

# ======================
# Optional: Wait for ETCD CA certificate
# ======================
if [[ -n "${ETCD_CA_CERT:-}" ]]; then
    echo " 🔐 ETCD_CA_CERT defined: waiting for CA file at $ETCD_CA_CERT"

    CA_WAIT_RETRIES="${ETCD_CA_WAIT_RETRIES:-50}"
    CA_WAIT_DELAY="${ETCD_CA_WAIT_DELAY_SEC:-0.2}"

    for i in $(seq 1 "$CA_WAIT_RETRIES"); do
        if [[ -s "$ETCD_CA_CERT" ]]; then
            echo "    ✅ CA certificate found."
            break
        fi
        echo "    ... waiting for CA file ($i/$CA_WAIT_RETRIES)"
        sleep "$CA_WAIT_DELAY"
    done

    if [[ ! -s "$ETCD_CA_CERT" ]]; then
        echo " ❌ ERROR: ETCD CA certificate not found at $ETCD_CA_CERT after waiting."
        exit 1
    fi
fi

export ETCD_HOST="${ETCD_ENDPOINT%%:*}"
export ETCD_PORT="${ETCD_ENDPOINT##*:}"


# ======================
# Launch the internal agent
# ======================
echo " 🚀 Launching sat-agent.py ..."
/usr/bin/screen -S APP -s /bin/bash -t win0 -A -d -m
sleep 1
screen -S APP -p win0 -X stuff $'python3 -u /app/sat-agent.py \n'
sleep infinity
