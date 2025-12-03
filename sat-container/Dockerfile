# ================================
# shahramdd/sat:8.6
# ================================
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    ETCD_ENDPOINT=10.0.1.215:2379 \
    SAT_NAME="name" \
    UPDATE_LINK_SH="/agent/update-link-internal.sh"

RUN apt update && apt install -y \
      iputils-ping iproute2 net-tools tcpdump bridge-utils \
      nano vim curl gnupg lsb-release ca-certificates \
      python3 python3-pip iperf3 jq \
      openssh-client sshpass openssh-server \
  && mkdir -p /var/run/sshd \
  && curl -s https://deb.frrouting.org/frr/keys.asc | gpg --dearmor -o /etc/apt/trusted.gpg.d/frr.gpg \
  && echo "deb https://deb.frrouting.org/frr $(lsb_release -cs) frr-stable" > /etc/apt/sources.list.d/frr.list \
  && apt update && apt install -y frr frr-pythontools \
  && pip3 install --no-cache-dir "protobuf==3.20.*" etcd3 \
  && ssh-keygen -A \
  && mkdir -p /var/log/frr \
  && chown frr:frr /var/log/frr || true \
  && rm -rf /var/lib/apt/lists/*

# Copy Scripts
COPY create_bridges.sh /usr/local/bin/create_bridges.sh
COPY sat-agent-internal.py /agent/sat-agent-internal.py
COPY update-link-internal.sh /agent/update-link-internal.sh
COPY configure-isis.sh /agent/configure-isis.sh

RUN chmod +x /usr/local/bin/create_bridges.sh && chmod +x /agent/*.sh

HEALTHCHECK --interval=30s --timeout=5s --retries=5 CMD \
  bash -lc 'ip link show br1 >/dev/null 2>&1 && pgrep -f "/agent/sat-agent-internal.py" >/dev/null 2>&1 || exit 1'

ENTRYPOINT ["/usr/local/bin/create_bridges.sh"]