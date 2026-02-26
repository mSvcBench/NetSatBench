#!/usr/bin/env python3
import argparse
import json
import logging
import os
import re
import socket
import subprocess
import time
from typing import Any, Dict, List, Tuple

# ----------------------------
#   GLOBALS & CONSTANTS
# ----------------------------

DEV_RE = re.compile(r"\bdev\s+([^\s]+)\b")
user_list=[]

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


def build_srv6_route_replace(dst_prefix: str, sids: str, dev: str) -> List[str]:
    return [
        "ip", "-6", "route", "replace", dst_prefix,
        "encap", "seg6", "mode", "encap", "segs", sids,
        "dev", dev,
    ]


def send_udp_json(sock: socket.socket, msg: Dict[str, Any], peer: Tuple[str, int, int, int]) -> None:
    data = json.dumps(msg).encode("utf-8")
    sock.sendto(data, peer)

# ----------------------------
#   MAIN HANDOVER LOGIC
# ----------------------------

def handle_registration_request(
    sock: socket.socket,
    payload: Dict[str, Any],
    peer: Tuple[str, int, int, int],
    local_ipv6: str,
) -> None:
    # Optional: tiny yield to let the packet enqueue before re-route.
    # (Not strictly required, but can help in very tight emulation timelines.)
    time.sleep(0.001)

    # Apply route change on grd to steer traffic to usr via new satellite
    user_id = payload["user_id"]
    user_ipv6 = payload["user_ipv6"]        
    init = payload["init_sat_ipv6"]          
    dev = derive_egress_dev(init)
    
    ## build traffic engineered path
    downstream_sids = init # dummy example with single SID, can be extended to multiple SIDs if needed (e.g., "sid1,sid2,...")
    upstream_sids = init+","+local_ipv6 # dummy example with single SID, can be extended to multiple SIDs if needed (e.g., "sid1,sid2,...")

    ip_cmd = build_srv6_route_replace(dst_prefix = user_ipv6, sids = downstream_sids, dev = dev)
    run_cmd(ip_cmd)
    logging.info(f"📡 Received registration request from {user_id}")

    # Sending registration_accept to usr with the sids to use 
    callback_port = payload.get("callback_port", 5005)  # Optional port to send registration_accept back to usr (default: 5005)
    txid = payload.get("txid", str(int(time.time() * 1000))) # nonce txid for correlation (default: current timestamp in ms)
    cmd_msg = {
        "type": "registration_accept",
        "txid": txid,
        "grd_id": os.environ["NODE_NAME"],  
        "grd_ipv6": local_ipv6,  # IPv6 usr should use to reach grd (e.g., "2001:db8:100::2/128")
        "sids": upstream_sids,  # SID usr must use to reach grd
    }
    logging.info(f"✉️ Sent registration accept to {user_id} with sid={upstream_sids}")
    peer_for_cmd = (peer[0], callback_port, peer[2], peer[3])  # keep flowinfo/scopeid
    send_udp_json(sock, cmd_msg, peer_for_cmd)

def handle_handover_request(
    sock: socket.socket,
    payload: Dict[str, Any],
    peer: Tuple[str, int, int, int],
    local_ipv6: str,
    ho_delay_ms: float
) -> None:
    # Optional: tiny yield to let the packet enqueue before re-route.
    # (Not strictly required, but can help in very tight emulation timelines.)
    time.sleep(0.001)

    # Sending handover command to usr with the sids to use 
    callback_port = payload.get("callback_port", 5005)  # Optional port to send registration_accept back to usr (default: 5005)
    txid = payload.get("txid", str(int(time.time() * 1000))) # nonce txid for correlation (default: current timestamp in ms)

    user_id = payload["user_id"]
    new_sat_ipv6 = payload["new_sat_ipv6"]  # In this simplified example, we directly use the new satellite as the SID. In a real scenario, the SID might be different and may require additional logic to determine.
    
    # Compute traffic engineered path if needed (e.g., based on new_sat_ipv6, network policies, etc.)
    upstream_sids = new_sat_ipv6+","+local_ipv6 # dummy example with single SID, can be extended to multiple SIDs if needed (e.g., "sid1,sid2,...")

    cmd_msg = {
        "type": "handover_command",
        "txid": txid,
        "grd_id": os.environ["NODE_NAME"],  
        "grd_ipv6": local_ipv6,  # IPv6 usr should use to reach grd (e.g., "2001:db8:100::2/128")
        "sids": upstream_sids,  # SID usr must use to reach grd
    }

    logging.info(f"✉️ Sent handover command to user {user_id} with sid={upstream_sids}")
    peer_for_cmd = (peer[0], callback_port, peer[2], peer[3])  # keep flowinfo/scopeid
    send_udp_json(sock, cmd_msg, peer_for_cmd)

    # Apply handover delay pause if configured (e.g., to allow user to switch satellite link or send back handover complete) as rate reduction to delay the packet scheduling on the new route
    if ho_delay_ms > 0:
        mtu = 1500  # Assuming MTU for shaping rules
        logging.info("⧴ Applying handover delay of %dms", ho_delay_ms)
        
        rate_kbit = max(1, int(mtu * 8 / ho_delay_ms))  # kbit/s (since ms in denominator)
        burst_bytes = mtu * 2
        cburst_bytes = mtu * 2
        idx = user_list.index(payload["user_id"])

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

    # Apply route change on to steer traffic on new route
    user_ipv6 = payload["user_ipv6"]        
    new_sat_ipv6 = payload["new_sat_ipv6"]
    dev = derive_egress_dev(new_sat_ipv6)
    
    # Compute traffic engineered path if needed (e.g., based on new_sat_ipv6, network policies, etc.)
    downstream_sids = new_sat_ipv6 # dummy example with single SID, can be extended to multiple SIDs if needed (e.g., "sid1,sid2,...")          
    ip_cmd = build_srv6_route_replace(dst_prefix=user_ipv6, sids=downstream_sids, dev=dev)
    run_cmd(ip_cmd)

def handle_request(
    sock: socket.socket,
    payload: Dict[str, Any],
    peer: Tuple[str, int, int, int],
    local_ipv6: str,
    ho_delay_ms: float,
) -> None:
    # Validate
    if payload.get("type") == "handover_request":
        handle_handover_request(sock, payload, peer, local_ipv6, ho_delay_ms)
    elif payload.get("type") == "registration_request":
        handle_registration_request(sock, payload, peer, local_ipv6)
    else:
        logging.warning("❌ Unknown command type: %s", payload.get("type", "N/A"))

def prepare_qdisc_for_new_user(user_ipv6: str, user_id: str) -> None:
    dev = "veth0_rt" # Assuming this is the shaping interface
    dst = user_ipv6.split("/")[0]  # Extract IP from possible prefix
    # derive user id as the position of username in the user_list 
    idx = user_list.index(user_id)
    run_cmd(["tc", "class", "add", "dev", dev, "parent", "1:", "classid", f"1:{idx+10}", "htb", "rate", "10gbit", "ceil", "10gbit"])
    run_cmd(["tc", "filter", "add", "dev", dev, "parent", "1:", "protocol", "ipv6", "prio", "10", "flower","dst_ip" ,dst, "action","pass","flowid" ,f"1:{idx+10}"])
    logging.info(f"🎛️ Applied created shaping qdisc and filter for {user_id}, prefix {user_ipv6}, on dev {dev}")

def init_qdisc() -> None:
    dev = "veth0_rt" # Assuming this is the shaping interfcace
    try:
        run_cmd(["tc", "qdisc", "del", "dev", dev, "root"])
    except:
        pass
    run_cmd(["tc", "qdisc", "add", "dev", dev, "root", "handle", "1:", "htb", "default", "1"])
    run_cmd(["tc", "class", "add", "dev", dev, "parent", "1:", "classid", "1:1", "htb", "rate", "10gbit", "ceil", "10gbit"])
    logging.info(f"🎛️ Initialized root qdisc on dev {dev} for handover delay shaping")
    
def serve(bind_addr: str, port: int, ho_delay: float, local_ipv6: str) -> None:
    # prepare qdisk for users (if ho_delay is set)
    if ho_delay > 0:
        init_qdisc()
    sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    sock.bind((bind_addr, port))
    logging.info("⚙️ Ground connection agent listening on [%s]:%d", bind_addr, port)

    while True:
        data, peer = sock.recvfrom(4096)
        try:
            msg = json.loads(data.decode("utf-8"))
            user_id = msg.get("user_id", "unknown")
            if user_id not in user_list:
                # add new user to user list and prepare qdisc for them
                user_list.append(user_id)
                if ho_delay > 0:
                    prepare_qdisc_for_new_user(user_ipv6=msg.get("user_ipv6"), user_id=user_id)
            logging.debug("RX from [%s]:%d msg=%s", peer[0], peer[1], msg)
            handle_request(sock=sock, payload=msg, peer=peer, ho_delay_ms=ho_delay, local_ipv6=local_ipv6)
        except Exception as e:
            logging.warning("❌ Request failed from [%s]:%d: %s", peer[0], peer[1], e)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bind", default="::", help="Address to bind the UDP server for handover (default: :: for all interfaces)")
    ap.add_argument("--port", type=int, default=5005, help="UDP port where grd listens for handover_request (default: 5005)")
    ap.add_argument("--local-address", help="IPv6 address of local node (Default: address found in /etc/hosts for the hostname)")
    ap.add_argument("--ho-delay", type=float, help="Handover delay in mseconds (requires veth0_rt interface, use app/shaping-ns-create-v6.sh)", default=0)
    args = ap.parse_args()
    
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    
    if args.local_address is None:
        # Derive local IPv6 address from the loopback interface
        # cat /etc/hosts | grep $HOSTNAME
        local_ipv6 = run_cmd_capture(["grep", os.environ["HOSTNAME"], "/etc/hosts"]).split()[0]
        logging.debug("Derived local IPv6 address from /etc/hosts: %s", local_ipv6)
    else:
        local_ipv6 = args.local_address
        logging.debug("Using provided local IPv6 address: %s", local_ipv6)

    serve(bind_addr=args.bind, port=args.port, ho_delay=args.ho_delay, local_ipv6=local_ipv6)


if __name__ == "__main__":
    main()
