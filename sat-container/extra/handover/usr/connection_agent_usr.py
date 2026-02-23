#!/usr/bin/env python3
import argparse
import json
import logging
import re
import socket
import subprocess
import time
from typing import Any, Dict, List, Tuple

# ----------------------------
# GLOBALS
# ----------------------------
grd_list = []
DEV_RE = re.compile(r"\bdev\s+([^\s]+)\b")

# ----------------------------
#   HELPERS
# ----------------------------

def sleep_until(deadline_ns: int, spin_ns: int = 200_000):  # spin last 0.2ms by default
    """Sleep until a monotonic deadline (ns)."""
    while True:
        now = time.monotonic_ns()
        remaining = deadline_ns - now
        if remaining <= 0:
            return
        # Sleep most of the remaining time
        if remaining > spin_ns:
            time.sleep((remaining - spin_ns) / 1e9)
        else:
            # Busy-wait for the final slice (more precise)
            pass

def run_cmd_capture(cmd: List[str]) -> str:
    res = subprocess.run(cmd, text=True, capture_output=True)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or f"Command failed: {' '.join(cmd)}")
    return res.stdout.strip()


def run_cmd(cmd: List[str]) -> None:
    logging.debug("EXEC: %s", " ".join(cmd))
    res = subprocess.run(cmd, text=True, capture_output=True)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or f"Command failed: {' '.join(cmd)}")


def derive_egress_dev(addr: str) -> str:
    out = run_cmd_capture(["ip", "-6", "route", "get", addr])
    m = DEV_RE.search(out)
    if not m:
        raise RuntimeError(f"Could not parse egress dev from: {out}")
    return m.group(1)


def build_srv6_route_replace(dst_prefix: str, sid: str, dev: str) -> List[str]:
    return [
        "ip", "-6", "route", "replace", dst_prefix,
        "encap", "seg6", "mode", "inline", "segs", sid,
        "dev", dev,
    ]

def init_qdisc() -> None:
    dev = "veth0_rt" # Assuming this is the shaping interfcace
    try:
        run_cmd(["tc", "qdisc", "del", "dev", dev, "root"])
    except:
        pass
    run_cmd(["tc", "qdisc", "add", "dev", dev, "root", "handle", "1:", "htb", "default", "20"])
    run_cmd(["tc", "class", "add", "dev", dev, "parent", "1:", "classid", "1:1", "htb", "rate", "10gbit", "ceil", "10gbit"])

def prepare_qdisc_for_grd(grd_prefix: str, grd_id: str) -> None:
    dev = "veth0_rt" # Assuming this is the shaping interface
    dst = grd_prefix.split("/")[0]  # Extract IP from prefix
    # derive user id as the position of username in the user_list 
    idx = grd_list.index(grd_id)
    run_cmd(["tc", "class", "add", "dev", dev, "parent", "1:", "classid", f"1:{idx+10}", "htb", "rate", "10gbit", "ceil", "10gbit"])
    run_cmd(["tc", "filter", "add", "dev", dev, "parent", "1:", "protocol", "ipv6", "prio", "10", "flower","dst_ip" ,dst, "action","pass","flowid" ,f"1:{idx+10}"])
    logging.info(f"🎛️ Applied created shaping qdisc and filter for {grd_id}, prefix {grd_prefix}, on dev {dev}")       

# ----------------------------
#   MAIN LOGIC
# ----------------------------

def handle_handover_command(payload: Dict[str, Any], ho_delay_ms: float) -> None:
   
    grd_prefix = payload["grd_prefix"]  # e.g. "2001:db8:101::1/128"
    upstream_sids = payload["sids"]                  # new sid sequence for the user (e.g., "2001:db8:200::1")
    last_sid = upstream_sids.split(",")[-1]          # last SID is the new egress SID to reach the grd. Shall be the IP address of a connected sat
    dev = derive_egress_dev(last_sid)
    logging.debug("Derived egress dev for SID %s: %s", upstream_sids, dev)

    # Apply handover delay rate reduction if configured (e.g., to allow user to switch satellite link)
    if ho_delay_ms > 0:
        logging.info("⧴ Applying handover delay of %dms", ho_delay_ms)
        mtu = 1500  # Assuming MTU for shaping rules
        rate_kbit = max(1, int(mtu * 8 / ho_delay_ms))  # kbit/s (since ms in denominator)
        burst_bytes = mtu * 2
        cburst_bytes = mtu * 2
        idx = grd_list.index(payload["grd_id"])

        run_cmd([
        "tc","class","change","dev","veth0_rt",
        "parent","1:","classid",f"1:{idx+10}",
        "htb",
        "rate",f"{rate_kbit}kbit","ceil",f"{rate_kbit}kbit",
        "burst",f"{burst_bytes}b","cburst",f"{cburst_bytes}b",
        ])

        deadline = time.monotonic_ns() + int(ho_delay_ms * 1_000_000)
        sleep_until(deadline)
        
        # Restore original qdisc after delay
        run_cmd([
        "tc","class","change","dev","veth0_rt",
        "parent","1:","classid",f"1:{idx+10}",
        "htb",
        "rate","10gbit","ceil","10gbit",
        "burst","15kb","cburst","15kb",   # example “normal” values
        ])
        logging.info("⧴ Handover delay completed, restored original qdisc settings")
    
    ip_cmd = build_srv6_route_replace(grd_prefix, upstream_sids, dev)
    run_cmd(ip_cmd)
    
    logging.info("📡 Applied route replace for %s via %s dev %s", grd_prefix, upstream_sids, dev)

def handle_registration_accept(payload: Dict[str, Any]) -> None:
   
    grd_prefix = payload["grd_prefix"]  # e.g. "2001:db8:101::1/128"
    upstream_sids = payload["sids"]                  # new sid sequence for the user (e.g., "2001:db8:200::1")
    last_sid = upstream_sids.split(",")[-1]          # last SID is the new egress SID to reach the grd. Shall be the IP address of a connected sat
    dev = derive_egress_dev(last_sid)
    logging.debug("Derived egress dev for SID %s: %s", upstream_sids, dev)

    ip_cmd = build_srv6_route_replace(grd_prefix, upstream_sids, dev)
    run_cmd(ip_cmd)
    
    logging.info("📡 Applied route replace for %s via %s dev %s", grd_prefix, upstream_sids, dev)

def handle_command(payload: Dict[str, Any], ho_delay_ms: float, grd_id: int) -> None:
    mtu = 1500  # Assuming MTU for shaping rules
    if payload.get("type") == "handover_command":
        handle_handover_command(payload, ho_delay_ms)
        return
    elif payload.get("type") == "registration_accept":
        handle_registration_accept(payload)
    else:
        raise ValueError(f"Unsupported command type: {payload.get('type')}")

def serve(bind_addr: str, port: int, ho_delay: float) -> None:
    sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    sock.bind((bind_addr, port))
    logging.info("⚙️ usr_agent listening on [%s]:%d", bind_addr, port)

    # prepare qdisk for users (if ho_delay is set)
    if ho_delay > 0:
        init_qdisc()

    while True:
        data, peer = sock.recvfrom(4096)
        try:
            msg = json.loads(data.decode("utf-8"))
            grd_id = msg.get("grd_id", "unknown")
            if grd_id not in grd_list:
                # add new grd to grd list and prepare qdisc for them
                grd_list.append(grd_id)
                if ho_delay > 0:
                    prepare_qdisc_for_grd(grd_prefix=msg.get("grd_prefix"), grd_id=grd_id)
            logging.debug("RX from [%s]:%d msg=%s", peer[0], peer[1], msg)
            handle_command(msg, ho_delay_ms=ho_delay, grd_id=grd_id)
        except Exception as e:
            logging.warning("❌ Failed command from [%s]:%d: %s", peer[0], peer[1], e)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bind", default="::")
    ap.add_argument("--port", type=int, default=5006, help="UDP port where usr1 listens for handover_command")
    ap.add_argument("--ho-delay", type=float, help="Handover delay in mseconds (requires veth0_rt, use app/shaping-ns-create-v6.sh)", default=0)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    serve(bind_addr=args.bind, port=args.port, ho_delay=args.ho_delay)

if __name__ == "__main__":
    main()
