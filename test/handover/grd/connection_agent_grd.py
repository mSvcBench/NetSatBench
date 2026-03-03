#!/usr/bin/env python3
import argparse
import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
import etcd3
from typing import Any, Dict, List, Tuple

# ----------------------------
#   GLOBALS & CONSTANTS
# ----------------------------
ETCD_ENDPOINT = os.getenv("ETCD_ENDPOINT", "127.0.0.1:2379")
ETCD_USER = os.getenv("ETCD_USER", None)
ETCD_PASSWORD = os.getenv("ETCD_PASSWORD", None)
ETCD_CA_CERT = os.getenv("ETCD_CA_CERT", None)

if ":" in ETCD_ENDPOINT:
    h, p = ETCD_ENDPOINT.split(":", 1)
    ETCD_HOST, ETCD_PORT = h, int(p)
else:
    ETCD_HOST, ETCD_PORT = ETCD_ENDPOINT, 2379

node_name = os.getenv("NODE_NAME")
KEY_LINKS_PREFIX = f"/config/links/{node_name}/"
link_setup_delay_s = 0.2 # estimated time needed by sat-agent to setup relevat routes and interfaces after a link is added in etcd, used to delay registration after link event to increase chances that the link is fully setup in the sat-agent before registration attempt (which can reduce registration failures due to missing routes/interfaces in the sat-agent at the time of registration)
DEV_RE = re.compile(r"\bdev\s+([^\s]+)\b")
VIA_RE = re.compile(r"\bvia\s+([^\s]+)\b")
SEGS_RE = re.compile(r"\bsegs\s+\d+\s+\[\s*([^\]]+)\]")
user_db: Dict[str, Dict[str, str]] = {} # key: user_id, value: {"upstream_sids": str, "downstream_sids": str, "dev": str} (for tracking registered users and their current routes)
links_db: Dict[str, Dict[str, str]] = {}  # key dev_id, value: {"endpoint1": str, "endpoint2": str, ...}
link_duration_initial_value_s = 120  # initial value for link duration (sec)
is_local_handover_needed = None  # assign the handover strategy function to use for processing handover decisions based on links_db state (can be extended to more complex strategies as needed)
handover_processing_lock = threading.Lock()
handover_processing_running = False
handover_processing_pending = False
hosts_ipv6_cache: Dict[str, str] = {}
local_ipv6_for_commands = ""
user_callback_port_default = 5006

# ----------------------------
#   HELPERS
# ----------------------------

def get_etcd_client() -> etcd3.Etcd3Client:
    logging.info(f"📁 Connecting to Etcd at {ETCD_HOST}:{ETCD_PORT}...")
    while True:
        try:
            if ETCD_USER and ETCD_PASSWORD:
                etcd_client = etcd3.client(host=ETCD_HOST, port=ETCD_PORT, user=ETCD_USER, password=ETCD_PASSWORD, ca_cert=ETCD_CA_CERT)
            else:
                etcd_client = etcd3.client(host=ETCD_HOST, port=ETCD_PORT)
            etcd_client.status()  # Test connection, if fail will raise
            logging.info(f" ✅ Connected to Etcd at {ETCD_HOST}:{ETCD_PORT}.")
            return etcd_client
        except Exception as e:
            logging.warning(f" ❌ Failed to connect to Etcd at {ETCD_HOST}:{ETCD_PORT}: {e}, retry in 5 seconds...")
            time.sleep(5)

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
    res = subprocess.run(cmd, text=True, capture_output=True)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or f"Command failed: {' '.join(cmd)}")


def refresh_hosts_ipv6_cache() -> None:
    global hosts_ipv6_cache
    parsed: Dict[str, str] = {}
    with open("/etc/hosts", "r", encoding="utf-8", errors="ignore") as hosts_file:
        for raw_line in hosts_file:
            line = raw_line.split("#", 1)[0].strip()
            if not line:
                continue
            fields = line.split()
            if len(fields) < 2:
                continue
            ip = fields[0]
            if ":" not in ip:
                continue
            for alias in fields[1:]:
                parsed[alias] = ip
    hosts_ipv6_cache = parsed


def resolve_ipv6_from_hosts(hostname: str) -> str:
    ipv6 = hosts_ipv6_cache.get(hostname)
    if ipv6:
        return ipv6
    refresh_hosts_ipv6_cache()
    ipv6 = hosts_ipv6_cache.get(hostname)
    if ipv6:
        return ipv6
    raise RuntimeError(f"Could not derive IPv6 for endpoint '{hostname}' from /etc/hosts")


def derive_egress_dev(addr: str) -> Tuple[str, str]:
    out = run_cmd_capture(["ip", "-6", "route", "get", addr])
    dev_match = DEV_RE.search(out)
    via_match = VIA_RE.search(out)
    segs_match = SEGS_RE.search(out)
    if not dev_match:
        raise RuntimeError(f"Could not parse egress dev from: {out}")
    if via_match:
        return dev_match.group(1), via_match.group(1)
    if segs_match:
        first_sid = segs_match.group(1).strip().split()[0]
        return dev_match.group(1), first_sid
    raise RuntimeError(f"Could not parse next-hop via IP or seg6 SID from: {out}")


def build_srv6_route_replace(dst_prefix: str, sids: str, dev: str, metric: int = 20) -> List[str]:
        return [
            "ip", "-6", "route", "replace", dst_prefix,
            "encap", "seg6", "mode", "encap", "segs", sids,
            "dev", dev,
            "metric", str(metric)
        ]


def send_udp_json(sock: socket.socket, msg: Dict[str, Any], peer: Tuple[str, int, int, int]) -> None:
    data = json.dumps(msg).encode("utf-8")
    sock.sendto(data, peer)


def update_user_db(user_id: str, user_ipv6: str, upstream_sids: str, downstream_sids: str, dev: str, status: str) -> None:
    user_db[user_id] = {
        "user_ipv6": user_ipv6,
        "upstream_sids": upstream_sids,
        "downstream_sids": downstream_sids,
        "dev": dev,
        "status": status,
    }


def get_user_index(user_id: str) -> int:
    try:
        return list(user_db.keys()).index(user_id)
    except ValueError as e:
        raise RuntimeError(f"User '{user_id}' not tracked in user_db") from e


def has_link_local_via_route(dst_ipv6: str) -> bool:
    dst = dst_ipv6.split("/")[0]
    try:
        out = run_cmd_capture(["ip", "-6", "route", "show", f"{dst}/128"])
    except Exception:
        return False

    for line in out.splitlines():
        s = line.strip()
        if not s or "encap seg6" in s:
            continue
        via_match = VIA_RE.search(s)
        if via_match and via_match.group(1).lower().startswith("fe80:"):
            return True
    return False


def wait_for_link_local_via_route(dst_ipv6: str, timeout_s: float = 2.0, poll_s: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if has_link_local_via_route(dst_ipv6):
            return True
        time.sleep(poll_s)
    return has_link_local_via_route(dst_ipv6)

# ----------------------------
#   WATCHERS
# ----------------------------
def watch_link_actions_loop (etcd_client) -> None:
    global status, current_iface, current_link, new_link
    logging.info("👀 Watching /config/links (Dynamic Events)...")
    backoff = 1
    while True:
        cancel = None
        try:
            events_iterator, cancel = etcd_client.watch_prefix(KEY_LINKS_PREFIX)
            for event in events_iterator:
                if isinstance(event, etcd3.events.PutEvent):
                    handle_link_put_action(event)
                elif isinstance(event, etcd3.events.DeleteEvent):
                    handle_link_delete_action(event)
        except Exception as ex:
            logging.error("❌ Failed to watch link actions (will retry): %s", ex)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
        finally:
            if cancel is not None:
                try:
                    cancel()
                except Exception:
                    pass

# ----------------------------
#   MAIN LOGIC FOR LINK MANAGEMENT LOCAL SIDE
# ----------------------------
def handle_link_put_action(event):
    global user_db
    try:
        ## Process PutEvent (Add/Update)
        if not isinstance(event, etcd3.events.PutEvent):
            logging.warning("⚠️ Ignoring non-PutEvent for handover processing.")
            return
        link_dev = event.key.decode().split("/")[-1]  # Assuming key format includes dev_id at the end
        l = json.loads(event.value.decode())
        ep1, ep2 = l.get("endpoint1"), l.get("endpoint2")
        if ep1 != node_name and ep2 != node_name:
            logging.error(f" ❌ Link action {ep1}<->{ep2} not relevant to this node.")
            return
        remote_endpoint = ep2 if ep1 == node_name else ep1
        
        # Check if this is the current link (e.g., an update to the current link) or a new link
        if link_dev in links_db and links_db[link_dev].get("status") == "active":
            logging.info(f"🔄 Link update detected for {link_dev}: {ep1}<->{ep2}")
            links_db[link_dev]["last_updated"] = time.time()
            for key in l:
                links_db[link_dev][key] = l[key]
        else:
            logging.info(f"➕ New link detected for {link_dev}: {ep1}<->{ep2}") 
            links_db[link_dev] = l
            links_db[link_dev]["last_created"] = time.time()
            links_db[link_dev]["status"] = "active"
            links_db[link_dev]["last_duration"] = link_duration_initial_value_s  # e.g., 0 or None
            links_db[link_dev]["remote_endpoint_ipv6"] = resolve_ipv6_from_hosts(remote_endpoint) if remote_endpoint else ""
        
        # Evaluate handover decision asynchronously (non-blocking for watcher thread)
        schedule_local_handover_processing()
        return
    except Exception as ex:
        logging.error("❌ Failed to process link action event %s", ex)
        return

def handover_strategy_newest(user_id: str) -> Tuple[str, bool]:
    # Example handover strategy: always prefer the newest link (e.g., for testing purposes)
    active_devs = [(dev,l) for dev,l in links_db.items() if l.get("status") == "active"]
    if not active_devs:
        logging.warning(f"⚠️ No active links available for handover decision for user {user_id}")
        return "", False
    newest_dev = max(active_devs, key=lambda x: x[1].get("last_created", 0))
    if newest_dev[0] != user_db.get(user_id, {}).get("dev"):
        # Here you would implement the logic to trigger handover to the selected link (e.g., by sending a handover command to the user or updating routing policies accordingly)
        return newest_dev[0],True
    else:
        return newest_dev[0],False

def processing_local_handover() -> None:
    for user_id in user_db.keys():
        if user_db[user_id].get("status") != "registered":
            continue
        new_dev, local_handover_needed = is_local_handover_needed(user_id)
        if local_handover_needed:
            logging.info(f"🔀 Handover decision for user {user_id}: selected newest link {new_dev}")
            handle_local_handover(user_id, new_dev)
    return  # Placeholder for handover decision logic based on links_db state (e.g., if current link is degraded or a better link is available, trigger handover by sending command to user via handle_handover_request or other means)


def schedule_local_handover_processing() -> None:
    global handover_processing_running, handover_processing_pending
    with handover_processing_lock:
        handover_processing_pending = True
        if handover_processing_running:
            return
        handover_processing_running = True

    def _worker() -> None:
        global handover_processing_running, handover_processing_pending
        try:
            while True:
                with handover_processing_lock:
                    if not handover_processing_pending:
                        handover_processing_running = False
                        return
                    handover_processing_pending = False
                processing_local_handover()
        except Exception as e:
            logging.error("❌ Local handover async worker failed: %s", e)
            with handover_processing_lock:
                handover_processing_running = False

    threading.Thread(target=_worker, daemon=True, name="local-handover-processor").start()

def handle_local_handover(user_id,new_dev):
    global user_db
    # reroute user traffic to new link 
    user = user_db.get(user_id)
    user_ipv6 = user.get("user_ipv6", "")
    old_dev = user.get("dev", "")
    old_downstream_sids = user_db.get(user_id, {}).get("downstream_sids", "") # dummy example with single SID, can be extended to multiple SIDs if needed (e.g., "sid1,sid2,...")
    new_downstream_sids = old_downstream_sids  # keep a copy of old downstream SIDs for potential rollback in case of failure
    new_dev_ipv6 = links_db.get(new_dev, {}).get("remote_endpoint_ipv6", "")
    new_downstream_sids_split = new_downstream_sids.split(",")
    if new_downstream_sids_split:
        new_downstream_sids_split[0] = new_dev_ipv6  # update first SID with new dev ipv6 (assuming the first SID is the egress SID towards the user, which should be updated to reflect the new link)
        new_downstream_sids = ",".join(new_downstream_sids_split)
    else:
        new_downstream_sids = new_dev_ipv6  # if no existing SIDs, just use the new dev ipv6 as the SID
    try:
        if not wait_for_link_local_via_route(new_dev_ipv6, timeout_s=link_setup_delay_s):
                logging.warning(
                    f"⚠️ No route with link-local next-hop for {new_dev_ipv6} before local handover"
                )
        ip_cmd = build_srv6_route_replace(dst_prefix=user_ipv6, sids=new_downstream_sids, dev=new_dev, metric=20)
        run_cmd(ip_cmd)
        user_db[user_id]["dev"] = new_dev 
        user_db[user_id]["downstream_sids"] = new_downstream_sids
        if user_id == node_name:
            logging.info(f"✅ Local handover completed for user {user_id} to new link on dev {new_dev}")
            return  # skip sending handover command to self in case of local handover for the satellite subnet
        
        ## send handover command to user to update upstream SIDs with new dev ipv6
        old_upstream_sids = user_db.get(user_id, {}).get("upstream_sids", "")
        upstream_parts = old_upstream_sids.split(",") if old_upstream_sids else []
        if upstream_parts:
            upstream_parts[-1] = new_dev_ipv6
            new_upstream_sids = ",".join(upstream_parts)
        else:
            new_upstream_sids = f"{new_dev_ipv6},{local_ipv6_for_commands}" if local_ipv6_for_commands else new_dev_ipv6
        user_db[user_id]["upstream_sids"] = new_upstream_sids

        user_ipv6 = user_db.get(user_id, {}).get("user_ipv6", "")
        if user_ipv6:
            callback_port = user_callback_port_default
            txid = str(int(time.time() * 1000))
            cmd_msg = {
                "type": "handover_command_unsolicited",
                "txid": txid,
                "grd_id": os.environ["NODE_NAME"],
                "grd_ipv6": local_ipv6_for_commands,
                "sids": new_upstream_sids,
            }
            with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as notify_sock:
                send_udp_json(notify_sock, cmd_msg, (user_ipv6, callback_port, 0, 0))
            logging.info(f"✉️ Sent local handover command to user {user_id} with sid={new_upstream_sids}")
        else:
            logging.warning(f"⚠️ Local handover completed for user {user_id}, but no user address is known to send handover_command.")
        logging.info(f"✅ Local handover completed for user {user_id} to new link on dev {new_dev}")
    except Exception as e:        
        logging.error(f"❌ Local handover failed for user {user_id} to new link on dev {new_dev}: {e}")
        restore_cmd = build_srv6_route_replace(dst_prefix=user_ipv6, sids=old_downstream_sids, dev=old_dev, metric=20)
        logging.info(f"🔄 Attempting to restore old route for user {user_id} on dev {old_dev}")
        try:
            run_cmd(restore_cmd)
            user_db[user_id]["dev"] = old_dev
            user_db[user_id]["downstream_sids"] = old_downstream_sids
            logging.info(f"🔄 Restored old route for user {user_id}")
        except Exception as e:
            user_db[user_id]["dev"] = ""
            user_db[user_id]["downstream_sids"] = ""
            logging.error(f"❌ Failed to restore old route for user {user_id}: {e}")
    return

def handle_link_delete_action(event):
    global user_db
    try:
        ## Process DeleteEvent (Remove)
        if not isinstance(event, etcd3.events.DeleteEvent):
            logging.warning("⚠️ Ignoring non-DeleteEvent for handover processing.")
            return
        link_dev = event.key.decode().split("/")[-1]  # Assuming key format includes dev_id at the end
        if link_dev in links_db:
            logging.info(f"➖ Link deleted for {link_dev}: {links_db[link_dev].get('endpoint1')}<->{links_db[link_dev].get('endpoint2')}")
            links_db[link_dev]["status"] = "terminated"
            links_db[link_dev]["last_duration"] = time.time() - links_db[link_dev].get("last_created", time.time())
            # Evaluate handover decision asynchronously (non-blocking for watcher thread)
            schedule_local_handover_processing()  # no need to delay processing for link deletion as we want to react as fast as possible to reroute users away from the deleted link
        else:
            logging.warning(f"⚠️ Received delete event for unknown link device {link_dev}, ignoring.")
        return
    except Exception as ex:
        logging.error("❌ Failed to process link delete event %s", ex)
        return

# ----------------------------
#   MAIN LOGIC FOR LINK MANAGEMENT USER SIDE
# ----------------------------

def handle_user_registration_request(
    sock: socket.socket,
    payload: Dict[str, Any],
    peer: Tuple[str, int, int, int],
    local_ipv6: str,
) -> None:

    # Apply route change on grd to steer traffic to usr via new satellite
    user_id = payload["user_id"]
    user_ipv6 = payload["user_ipv6"]        
    init = payload["init_sat_ipv6"]          
    dev, dev_ipv6 = derive_egress_dev(init)
    logging.info(f"👤 Received registration request from {user_id}")
    
    ## build traffic engineered path
    downstream_sids = dev_ipv6+","+init # dummy example with single SID, can be extended to multiple SIDs if needed (e.g., "sid1,sid2,...")
    upstream_sids = init+","+local_ipv6 # dummy example with single SID, can be extended to multiple SIDs if needed (e.g., "sid1,sid2,...")
    ip_cmd = build_srv6_route_replace(dst_prefix = user_ipv6, sids = downstream_sids, dev = dev)
    run_cmd(ip_cmd)
    

    # Sending registration_accept to usr with the sids to use 
    callback_port = payload.get("callback_port", user_callback_port_default)  # Optional port to send registration_accept back to usr
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

    update_user_db(
        user_id=user_id,
        user_ipv6=user_ipv6,
        upstream_sids=upstream_sids,
        downstream_sids=downstream_sids,
        dev=dev,
        status="registered"
    )


def handle_user_handover_request(
    sock: socket.socket,
    payload: Dict[str, Any],
    peer: Tuple[str, int, int, int],
    local_ipv6: str,
    ho_delay_ms: float
) -> None:

    # Sending handover command to usr with the sids to use 
    callback_port = payload.get("callback_port", user_callback_port_default)  # Optional port to send handover_command back to usr
    txid = payload.get("txid", str(int(time.time() * 1000))) # nonce txid for correlation (default: current timestamp in ms)

    user_id = payload["user_id"]
    user_ipv6 = payload["user_ipv6"]
    new_sat_ipv6 = payload["new_sat_ipv6"]  # In this simplified example, we directly use the new satellite as the SID. In a real scenario, the SID might be different and may require additional logic to determine.
    
    logging.info(f"🔀 Received handover request from {user_id} for new satellite {new_sat_ipv6}")
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
    user_db[user_id]["status"] = "handover_in_progress"  # Update user_db with new status for the user after sending handover command

    # Apply handover delay pause if configured (e.g., to allow user to switch satellite link or send back handover complete) as rate reduction to delay the packet scheduling on the new route
    if ho_delay_ms > 0:
        mtu = 1500  # Assuming MTU for shaping rules
        logging.info("⧴ Applying handover delay of %dms", ho_delay_ms)
        
        rate_kbit = max(1, int(mtu * 8 / ho_delay_ms))  # kbit/s (since ms in denominator)
        burst_bytes = mtu * 2
        cburst_bytes = mtu * 2
        idx = get_user_index(payload["user_id"])

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

    # Apply route change on to steer downstream traffic on new route
    dev, dev_ipv6 = derive_egress_dev(new_sat_ipv6)
    
    # Compute traffic engineered path if needed (e.g., based on new_sat_ipv6, network policies, etc.)
    downstream_sids = dev_ipv6+","+new_sat_ipv6 # dummy example with single SID, can be extended to multiple SIDs if needed (e.g., "sid1,sid2,...")
    ip_cmd = build_srv6_route_replace(dst_prefix=user_ipv6, sids=downstream_sids, dev=dev)
    run_cmd(ip_cmd)
    
    update_user_db(
        user_id=user_id,
        user_ipv6=user_ipv6,
        upstream_sids=upstream_sids,
        downstream_sids=downstream_sids,
        dev=dev,
        status="registered"
    )
    
def handle_user_request(
    sock: socket.socket,
    payload: Dict[str, Any],
    peer: Tuple[str, int, int, int],
    local_ipv6: str,
    ho_delay_ms: float,
) -> None:
    # Validate
    if payload.get("type") == "handover_request":
        threading.Thread(
            target=handle_user_handover_request,
            args=(sock, dict(payload), peer, local_ipv6, ho_delay_ms),
            daemon=True,
            name=f"ho-handler-{payload.get('user_id', 'unknown')}",
        ).start()
    elif payload.get("type") == "registration_request":
        threading.Thread(
            target=handle_user_registration_request,
            args=(sock, dict(payload), peer, local_ipv6),
            daemon=True,
            name=f"reg-handler-{payload.get('user_id', 'unknown')}",
        ).start()
    else:
        logging.warning("❌ Unknown command type: %s", payload.get("type", "N/A"))

def prepare_qdisc_for_new_user(user_ipv6: str, user_id: str) -> None:
    dev = "veth0_rt" # Assuming this is the shaping interface
    dst = user_ipv6.split("/")[0]  # Extract IP from possible prefix
    # derive user index from insertion order in user_srv6_route_state
    idx = get_user_index(user_id)
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
            if user_id not in user_db:
                # Track user as soon as first message arrives
                user_db[user_id] = {
                    "user_ipv6": msg.get("user_ipv6", ""),
                    "upstream_sids": "",
                    "downstream_sids": "",
                    "dev": "",
                    "status": "not-registered",
                }
                if ho_delay > 0:
                    prepare_qdisc_for_new_user(user_ipv6=msg.get("user_ipv6"), user_id=user_id)
            user_db[user_id]["peer_addr"] = peer[0]
            user_db[user_id]["peer_flowinfo"] = peer[2]
            user_db[user_id]["peer_scopeid"] = peer[3]
            if "callback_port" in msg:
                user_db[user_id]["callback_port"] = int(msg["callback_port"])
            handle_user_request(sock=sock, payload=msg, peer=peer, ho_delay_ms=ho_delay, local_ipv6=local_ipv6)
        except Exception as e:
            logging.warning("❌ Request failed from [%s]:%d: %s", peer[0], peer[1], e)


def main() -> None:
    global is_local_handover_needed, link_setup_delay_s, local_ipv6_for_commands, user_callback_port_default
    ap = argparse.ArgumentParser()
    ap.add_argument("--bind", default="::", help="Address to bind the UDP server for handover (default: :: for all interfaces)")
    ap.add_argument("--port", type=int, default=5005, help="UDP port where grd listens for handover_request (default: 5005)")
    ap.add_argument("--local-address", help="IPv6 address of local node (Default: address found in /etc/hosts for the hostname)")
    ap.add_argument("--ho-strategy", choices=["newest"], default="newest", help="Handover strategy to use for local handover decision processing based on links_db state (default: newest)")
    ap.add_argument("--ho-delay", type=float, help="Handover delay in mseconds (requires veth0_rt interface, use app/shaping-ns-create-v6.sh)", default=0)
    ap.add_argument("--usr-port", type=int, default=5006, help="Default UDP port where user agent listens for commands (default: 5006)")
    ap.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"), help="Logging level (default: INFO or value of LOG_LEVEL env var)")
    ap.add_argument("--sat-ipv6-prefix", default="2001:db8:100::/64", help="IPv6 prefix used for satellite SIDs (default: 2001:db8:100::/64)    ")
    ap.add_argument("--link-setup-delay", type=float, default=5, help="Estimated time in seconds needed by to setup relevat routes and interfaces after link creatio, default 5s)")
    args = ap.parse_args()
    
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(message)s")
    refresh_hosts_ipv6_cache()
    
    if args.local_address is None:
        # Derive local IPv6 address from the loopback interface
        local_ipv6 = run_cmd_capture(["grep", os.environ["NODE_NAME"], "/etc/hosts"]).split()[0]
        logging.debug("Derived local IPv6 address from /etc/hosts: %s", local_ipv6)
    else:
        local_ipv6 = args.local_address
        logging.debug("Using provided local IPv6 address: %s", local_ipv6)
    local_ipv6_for_commands = local_ipv6
    user_callback_port_default = args.usr_port
    # Set handover strategy function based on argument
    
    if args.ho_strategy == "newest":
        is_local_handover_needed = handover_strategy_newest
    else:        
        logging.error(f"Unsupported handover strategy: {args.ho_strategy}")
        sys.exit(1)
    
    # Start watching link actions in a separate thread
    etcd_client = get_etcd_client()
    link_setup_delay_s = args.link_setup_delay

    # Add grd to user_db to use handover stratey for the default route towards satellites. The route is stored in downstream_sids 
    
    user_db[os.environ["NODE_NAME"]] = {
        "user_ipv6": args.sat_ipv6_prefix,  # example IPv6 for the grd default route towards satellites (can be adjusted as needed)
        "upstream_sids": "",
        "downstream_sids": "",
        "dev": "",
        "status": "registered",
    }
    
    # configure default route for satellites
    new_dev, local_handover_needed = is_local_handover_needed(os.environ["NODE_NAME"])  # trigger initial handover decision for default route towards satellites based on initial links_db state (if any)
    if local_handover_needed:
        logging.info(f"🔀 Initial handover decision for grd default route towards satellites: selected newest link {new_dev}")
        handle_local_handover(os.environ["NODE_NAME"], new_dev)
    
    threading.Thread(
        target=watch_link_actions_loop,
        args=(etcd_client,),
        daemon=True,
        name="watch-link-actions",
        ).start()
    
    # Start UDP server to handle user registration and handover requests
    serve(bind_addr=args.bind, port=args.port, ho_delay=args.ho_delay, local_ipv6=local_ipv6)


if __name__ == "__main__":
    main()
