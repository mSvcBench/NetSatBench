# ================================
# NetSatBench:0.1
# ================================
FROM python:3.11-slim

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    ETCD_ENDPOINT=127.0.0.1:2379 \
    SAT_NAME="sat" \
    # Updated Script Name
    LINK_ACTIONS_SH="/agent/link-actions.sh"

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl gnupg lsb-release ca-certificates && \
    curl -s https://deb.frrouting.org/frr/keys.asc | gpg --dearmor -o /etc/apt/trusted.gpg.d/frr.gpg && \
    echo "deb https://deb.frrouting.org/frr $(lsb_release -cs) frr-stable" > /etc/apt/sources.list.d/frr.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
      iputils-ping iproute2 net-tools tcpdump bridge-utils \
      iperf3 jq \
      frr frr-pythontools && \
    pip3 install --no-cache-dir etcd3 && \
    mkdir -p /var/log/frr && \
    chown frr:frr /var/log/frr || true && \
    rm -rf /var/lib/apt/lists/* /var/log/apt/* /var/log/dpkg.log

RUN mkdir -p /app
WORKDIR /app

# Copy Scripts
COPY start-sat-agent.sh /app/start-sat-agent.sh
COPY sat-agent.py /app/sat-agent.py
COPY link-actions.sh /app/link-actions.sh
COPY configure-isis.sh /app/configure-isis.sh

RUN chmod +x /app/start-sat-agent.sh && chmod +x /app/*.sh
HEALTHCHECK --interval=30s --timeout=5s --retries=5 CMD \
  bash -lc 'ip link show br1 >/dev/null 2>&1 && pgrep -f "/app/sat-agent.py" >/dev/null 2>&1 || exit 1'

CMD ["/app/start-sat-agent.sh"]  
# ================================
