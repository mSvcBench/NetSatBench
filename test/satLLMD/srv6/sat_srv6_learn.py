#!/usr/bin/env python3
"""SRv6 reverse-path learner.
For every received packet that carries an SRH with segments_left == 0, install
    <src>/128  encap seg6 mode inline segs <reverse list>
(reverse list = SRH entries Address[1..n-1]; Address[0] is this node).
Run inside the node's network namespace; needs NET_ADMIN.
"""
import argparse, ctypes, ipaddress, signal, socket, struct, subprocess, sys, time

ETH_P_IPV6 = 0x86DD
PACKET_HOST = 0
SO_ATTACH_FILTER = 26
SKF_AD_IFINDEX = 0xFFFFF000 + 8      # BPF ancillary load: index of the receiving interface


def run(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True)

def ip6(*args):
    return run("ip", "-6", *args)

def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def ensure_dev(name, fallback="eth0"):
    """Permanent placeholder interface for the learned routes (not loopback)."""
    if run("ip", "link", "show", name).returncode != 0:
        if run("ip", "link", "add", name, "type", "dummy").returncode != 0:
            log(f"cannot create {name}, using {fallback}")
            return fallback
    run("ip", "link", "set", name, "up")
    run("ip", "link", "set", name, "mtu", "1400")
    return name


def build_filter(prefix, exclude_if=None):
    """Classic BPF: not received on exclude_if, SRH directly after the IPv6 header
    with segments_left == 0, and (optionally) source inside prefix."""
    insns = []
    if exclude_if:
        insns += [[0x20, 0, 0, SKF_AD_IFINDEX],
                  [0x15, 0, 0, socket.if_nametoindex(exclude_if)]]   # equal -> drop
    insns += [[0x30, 0, 0, 6], [0x15, 0, 0, 43],     # next header == routing header
              [0x30, 0, 0, 42], [0x15, 0, 0, 4]]     # routing type == 4 (SRH)
    insns += [[0x30, 0, 0, 43], [0x15, 0, 0, 0]]      # segments left == 0
    if prefix:
        net = ipaddress.IPv6Network(prefix)
        addr, plen = net.network_address.packed, net.prefixlen
        for i in range((plen + 31) // 32):
            bits = min(32, plen - 32 * i)
            mask = (0xFFFFFFFF << (32 - bits)) & 0xFFFFFFFF
            val = struct.unpack("!I", addr[4 * i:4 * i + 4])[0] & mask
            insns += [[0x20, 0, 0, 8 + 4 * i], [0x54, 0, 0, mask], [0x15, 0, 0, val]]
    insns += [[0x06, 0, 0, 0xFFFF], [0x06, 0, 0, 0]]
    drop = len(insns) - 1
    for idx, ins in enumerate(insns):
        if ins[0] != 0x15:
            continue
        if exclude_if and idx == 1:
            ins[1] = drop - idx - 1             # jt: interface matches -> drop
        else:
            ins[2] = drop - idx - 1             # jf: test fails -> drop
    return insns


def attach_filter(sock, insns):
    raw = b"".join(struct.pack("HBBI", *i) for i in insns)
    buf = ctypes.create_string_buffer(raw, len(raw))
    sock.setsockopt(socket.SOL_SOCKET, SO_ATTACH_FILTER,
                    struct.pack("HL", len(insns), ctypes.addressof(buf)))
    return buf


def find_srh(pkt):
    """pkt starts at the IPv6 header; return the SRH bytes or None."""
    if len(pkt) < 40 or pkt[0] >> 4 != 6:
        return None
    nh, off = pkt[6], 40
    while nh in (0, 43, 60):                  # hop-by-hop, routing, dest options
        if len(pkt) < off + 8:
            return None
        hlen = (pkt[off + 1] + 1) * 8
        if nh == 43 and pkt[off + 2] == 4:    # routing header type 4 = SRH
            return pkt[off:off + hlen]
        nh, off = pkt[off], off + hlen
    return None


def parse_srh(srh):
    _, _, _, segleft, last, _, _ = struct.unpack("!BBBBBBH", srh[:8])
    n = last + 1
    if len(srh) < 8 + 16 * n:
        return None
    segs = [str(ipaddress.IPv6Address(srh[8 + 16 * i: 24 + 16 * i])) for i in range(n)]
    return segleft, segs


def link_local_nexthop_route(src):
    """Return the output of `ip -6 route get <src>` if the route actually used to reach src (longest prefix,
    default route included) has a link-local next hop (`via fe80::...`), else None. SRv6 routes do not count."""
    out = ip6("route", "get", src).stdout.strip()
    if not out or "encap seg6" in out:
        return None
    words = out.split()
    for i, w in enumerate(words[:-1]):
        if w == "via":
            try:
                return out.splitlines()[0] if ipaddress.IPv6Address(words[i + 1]).is_link_local else None
            except ValueError:
                return None
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", help="learn only sources inside this prefix (e.g. 2001:db8:102::/48)")
    ap.add_argument("--exclude-iface", default="eth0", help="ignore packets received on this interface (empty = none)")
    ap.add_argument("--dev", help="placeholder interface (default: create dummy sr0, fallback eth0)")
    ap.add_argument("--route-mtu", type=int, help="MTU to set on learned routes (leave room for the SRH)")
    ap.add_argument("--ttl", type=int, default=300, help="seconds without traffic before removing a route")
    ap.add_argument("--ttl-refresh", type=float, default=10,
                    help="period (s) of the check that removes expired routes, instead of checking at every packet; "
                         "a route is removed at most ttl + ttl-refresh seconds after its last packet (default 10)")
    a = ap.parse_args()

    only = ipaddress.IPv6Network(a.prefix) if a.prefix else None
    dev = a.dev or ensure_dev("sr0")

    s = socket.socket(socket.AF_PACKET, socket.SOCK_DGRAM, socket.htons(ETH_P_IPV6))
    keep = attach_filter(s, build_filter(a.prefix, a.exclude_iface))  # keep reference alive
    s.settimeout(1)

    table = {}                                          # src -> (segs, last_seen)
    skipped = {}                                        # src -> checked_at: reached via a link-local next hop, not learned
    SKIP_RECHECK_S = 10                                 # do not look at the routing table for every packet

    def cleanup(*_):
        for src in list(table):
            ip6("route", "del", f"{src}/128")
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    def learn(pkt, addr, now):
        if addr[2] != PACKET_HOST:
            return
        srh = find_srh(pkt)
        if not srh:
            return
        if srh[3] != 0:                                 # only the end node (segments_left == 0) learns
            return
        parsed = parse_srh(srh)
        if not parsed or len(parsed[1]) < 2:
            return
        src_ip = ipaddress.IPv6Address(pkt[8:24])
        if src_ip.is_link_local or src_ip.is_multicast or (only and src_ip not in only):
            return
        src = str(src_ip)
        rev = tuple(parsed[1][1:])                      # drop Address[0] (this node)
        old = table.get(src)
        if old is None:
            # do not learn a source reached through a link-local next hop (via fe80::...)
            if now - skipped.get(src, -SKIP_RECHECK_S) < SKIP_RECHECK_S:
                return
            existing = link_local_nexthop_route(src)
            if existing:
                if src not in skipped:
                    log(f"not learning {src}: route via link-local next hop ({existing})")
                skipped[src] = now
                return
            skipped.pop(src, None)
        if old is None or old[0] != rev:
            mtu = ["mtu", str(a.route_mtu)] if a.route_mtu else []
            r = ip6("route", "replace", f"{src}/128", "encap", "seg6", "mode", "inline",
                    "segs", ",".join(rev), "dev", dev, *mtu)
            if r.returncode != 0:
                log(f"route error for {src}: {r.stderr.strip()}")
                return
            log(f"learned {src} via {','.join(rev)} (from {addr[0]})")
        table[src] = (rev, now)

    log(f"started: dev={dev} prefix={a.prefix} exclude={a.exclude_iface} mtu={a.route_mtu} ttl={a.ttl}s "
        f"ttl-refresh={a.ttl_refresh}s")
    last_ageing = time.time()
    while True:
        try:
            pkt, addr = s.recvfrom(65535)
        except socket.timeout:
            pkt = None
        now = time.time()
        if pkt:
            learn(pkt, addr, now)
        if now - last_ageing < a.ttl_refresh:           # ageing: only every ttl-refresh seconds, not at every packet
            continue
        last_ageing = now
        for src, (rev, seen) in list(table.items()):
            if now - seen > a.ttl:
                ip6("route", "del", f"{src}/128")
                del table[src]
                log(f"expired {src}")


if __name__ == "__main__":
    main()