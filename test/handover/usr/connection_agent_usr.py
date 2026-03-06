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
from typing import Any, Dict, List, Tuple
import etcd3


# ----------------------------
# GLOBALS
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

grd_list = []
links_db = {} # link info indexed by iface name (e.g., "vl_sat2_1": {endpoint1, endpoint2, delay, vni, etc.})
hosts_ipv6_cache: Dict[str, str] = {}
DEV_RE = re.compile(r"\bdev\s+([^\s]+)\b")
VIA_RE = re.compile(r"\bvia\s+([^\s]+)\b")
node_name = os.getenv("NODE_NAME")
KEY_LINKS_PREFIX = f"/config/links/{node_name}/"
link_setup_delay_s = 0.2 # estimated time needed by sat-agent to setup relevat routes and interfaces after a link is added in etcd, used to delay registration after link event to increase chances that the link is fully setup in the sat-agent before registration attempt (which can reduce registration failures due to missing routes/interfaces in the sat-agent at the time of registration)
registration_accept_timeout_s = None
handover_command_timeout_s = None
handover_metadata = {} # metadata used by the handover strategy to evaluate handover eligibility
handover_periodic_check_s = 3.3 # periodic check interval for handover decision 
link_duration_initial_value_s = 4*60  # initial value for link duration (sec)

# Status not_registered, registration_in_progress, registered, handover_in_progress
status = "not_registered" # initial status before registration
current_dev = None # current iface used for data transfer
new_dev = None # new iface being considered for handover

# ho eligibility strategy function, set in main() based on args
is_handover_needed = None
grd_ipv6 = None
grd_port = None
grd_id = None
callback_port_runtime = None
local_ipv6 = None
etcd_client_runtime = None
registration_timeout_timer = None
handover_timeout_timer = None
_UNSET = object()

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


def send_udp_json(dst_ipv6: str, dst_port: int, msg: Dict[str, Any]) -> None:
    data = json.dumps(msg).encode("utf-8")
    sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    try:
        sock.sendto(data, (dst_ipv6, dst_port))
    finally:
        sock.close()


def send_registration_request_udp(
    grd_ipv6: str,
    grd_port: int,
    user_ipv6: str,
    callback_port: int,
    init_sat_ipv6: str,
) -> None:
    msg: Dict[str, Any] = {
        "type": "registration_request",
        "user_id": os.environ["NODE_NAME"],
        "user_ipv6": user_ipv6,
        "init_sat_ipv6": init_sat_ipv6,
        "callback_port": callback_port,
        "txid": str(int(time.time() * 1000)),
    }
    send_udp_json(grd_ipv6, grd_port, msg)


def send_handover_request_udp(
    grd_ipv6: str,
    port: int,
    user_ipv6: str,
    callback_port: int,
    new_sat_ipv6: str,
) -> None:
    msg: Dict[str, Any] = {
        "type": "handover_request",
        "user_id": os.environ["NODE_NAME"],
        "user_ipv6": user_ipv6,
        "new_sat_ipv6": new_sat_ipv6,
        "callback_port": callback_port,
        "txid": str(int(time.time() * 1000)),
    }
    send_udp_json(grd_ipv6, port, msg)


def derive_egress_dev(addr: str) -> str:
    out = run_cmd_capture(["ip", "-6", "route", "get", addr])
    dev_match = DEV_RE.search(out)
    if not dev_match:
        raise RuntimeError(f"Could not parse egress dev from: {out}")
    return dev_match.group(1)


def build_srv6_route_replace(dst_prefix: str, sid: str, dev: str) -> List[str]:
    return [
        "ip", "-6", "route", "replace", dst_prefix,
        "encap", "seg6", "mode", "encap", "segs", sid,
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

def prepare_qdisc_for_grd(grd_ipv6: str, grd_id: str) -> None:
    dev = "veth0_rt" # Assuming this is the shaping interface
    dst = grd_ipv6.split("/")[0]  # Extract IP from prefix
    # derive user id as the position of username in the user_list 
    idx = grd_list.index(grd_id)
    run_cmd(["tc", "class", "add", "dev", dev, "parent", "1:", "classid", f"1:{idx+10}", "htb", "rate", "10gbit", "ceil", "10gbit"])
    run_cmd(["tc", "filter", "add", "dev", dev, "parent", "1:", "protocol", "ipv6", "prio", "10", "flower","dst_ip" ,dst, "action","pass","flowid" ,f"1:{idx+10}"])
    logging.info(f"🎛️ Applied created shaping qdisc and filter for {grd_id}, prefix {grd_ipv6}, on dev {dev}")       


def build_ipv6_default_route(grd_ipv6: str) -> List[str]:
    return [
        "ip", "-6", "route", "replace", "default",
        "via", grd_ipv6.split("/")[0],  # Extract IP from prefix
    ]

def cancel_registration_timeout() -> None:
    global registration_timeout_timer
    if registration_timeout_timer is not None:
        registration_timeout_timer.cancel()
        registration_timeout_timer = None

def update_link_db(link_dev: str, etcd_link_data: Any = _UNSET, last_created: Any = _UNSET, last_updated: Any = _UNSET, status: Any = _UNSET, last_duration: Any = _UNSET, remote_endpoint_ipv6: Any = _UNSET) -> None:
    global links_db
    links_db.setdefault(link_dev, {})
    if etcd_link_data is not _UNSET and etcd_link_data is not None:
        for key in etcd_link_data:
            links_db[link_dev][key] = etcd_link_data[key]

    links_db[link_dev]["last_created"] = last_created if last_created is not _UNSET else links_db.get(link_dev, {}).get("last_created", None)
    links_db[link_dev]["last_updated"] = last_updated if last_updated is not _UNSET else links_db.get(link_dev, {}).get("last_updated", None)
    links_db[link_dev]["status"] = status if status is not _UNSET else links_db.get(link_dev, {}).get("status", None)
    links_db[link_dev]["last_duration"] = last_duration if last_duration is not _UNSET else links_db.get(link_dev, {}).get("last_duration", link_duration_initial_value_s)
    links_db[link_dev]["remote_endpoint_ipv6"] = remote_endpoint_ipv6 if remote_endpoint_ipv6 is not _UNSET else links_db.get(link_dev, {}).get("remote_endpoint_ipv6", None)

def on_registration_accept_timeout() -> None:
    global status, current_dev, etcd_client_runtime, registration_timeout_timer
    registration_timeout_timer = None
    if status != "registration_in_progress":
        return

    logging.warning("⏱️ Registration accept timeout reached. Resetting state and retrying registration.")
    status = "not_registered"
    current_dev = None

    if etcd_client_runtime is not None:
        handle_registration_request()

def start_registration_timeout() -> None:
    global registration_timeout_timer
    cancel_registration_timeout()
    registration_timeout_timer = threading.Timer(registration_accept_timeout_s, on_registration_accept_timeout)
    registration_timeout_timer.daemon = True
    registration_timeout_timer.start()

def cancel_handover_command_timeout() -> None:
    global handover_timeout_timer
    if handover_timeout_timer is not None:
        handover_timeout_timer.cancel()
        handover_timeout_timer = None

def on_handover_command_timeout() -> None:
    global status, new_dev
    if status != "handover_in_progress":
        return
    logging.warning("⏱️ Handover command timeout reached.")
    new_dev = None
    status = "registered" if status == "handover_in_progress" else status
    cancel_handover_command_timeout()
    return

def start_handover_command_timeout(timeout_s: float) -> None:
    global handover_timeout_timer
    cancel_handover_command_timeout()
    handover_timeout_timer = threading.Timer(timeout_s, on_handover_command_timeout)
    handover_timeout_timer.daemon = True
    handover_timeout_timer.start()

def parse_delay(delay) -> float:
    if isinstance(delay, (int, float)):
        return float(delay)
    elif isinstance(delay, str):
        delay = delay.strip().lower()
        if delay.endswith("ms"):
            return float(delay[:-2])
        elif delay.endswith("us"):
            return float(delay[:-2]) / 1000
        elif delay.endswith("s"):
            return float(delay[:-1]) * 1000
        else:
            raise ValueError(f"Unknown delay format: {delay}")
    else:
        raise ValueError(f"Invalid delay type: {type(delay)}")

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
    raise RuntimeError(f"❌ Could not derive IPv6 for endpoint '{hostname}' from /etc/hosts")


# ----------------------------
#   MAIN LOGIC
# ----------------------------

def preload_links_db_from_etcd(etcd_client) -> None:
    loaded = 0
    skipped = 0
    logging.info("📥 Preloading links state from Etcd prefix %s", KEY_LINKS_PREFIX)
    try:
        for value, metadata in etcd_client.get_prefix(KEY_LINKS_PREFIX):
            if not value:
                skipped += 1
                continue
            try:
                key = metadata.key.decode() if metadata and metadata.key else ""
                link_dev = key.split("/")[-1] if key else ""
                if not link_dev:
                    skipped += 1
                    continue
                l = json.loads(value.decode())
                ep1, ep2 = l.get("endpoint1"), l.get("endpoint2")
                if ep1 != node_name and ep2 != node_name:
                    skipped += 1
                    continue
                remote_endpoint = ep2 if ep1 == node_name else ep1
                remote_endpoint_ipv6 = resolve_ipv6_from_hosts(remote_endpoint) if remote_endpoint else ""
                update_link_db(link_dev=link_dev, etcd_link_data=l, last_created=time.time(), last_updated=time.time(), status="available", last_duration=link_duration_initial_value_s, remote_endpoint_ipv6=remote_endpoint_ipv6)
                loaded += 1
            except Exception as e:
                skipped += 1
                logging.warning("⚠️ Skipping malformed initial link entry: %s", e)
        logging.info("📥 Initial links preload completed: loaded=%d skipped=%d", loaded, skipped)
    except Exception as e:
        logging.warning("⚠️ Failed to preload initial links from Etcd: %s", e)

#  Registration
def handle_registration_request() -> None:
    """
    Reads /config/links and builds the initial world state.
    Uses 'add' action for everything found.
    """
    global status, current_dev
    
    if status != "not_registered":
        logging.warning(f"⚠️  Skipping registration request since status is {status}")
        return
    logging.info("🌍  Processing Registration Request...")
    
    ## Process initial registration using link with minimum delay (if any)
    init_dev, _ = is_handover_needed(handover_metadata) # chose of the initial dev to serve the user based on handover strategy 
    
    if init_dev != "":
        init_sat_ipv6 = links_db.get(init_dev, {}).get("remote_endpoint_ipv6", "")
        if not init_sat_ipv6:
            logging.info(f"❌ Failed to resolve access satellite IPv6 address for dev {init_dev}")
            return
        logging.info(f"🛰️ Found access link via {init_sat_ipv6} dev {init_dev}. Registering...")
        try:
            status = "registration_in_progress"
            # add route to grd via initial satellite to ensure registration request can reach the grd
            if not wait_for_link_local_via_route(init_sat_ipv6, timeout_s=link_setup_delay_s):
                logging.warning(
                    f"⚠️ No route with link-local next-hop for {init_sat_ipv6} before handover request timeout window."
                )
            ip_cmd = build_srv6_route_replace(grd_ipv6, init_sat_ipv6, init_dev)
            print(f"Running command to add srv6 route for registration: {' '.join(ip_cmd)}")
            run_cmd(ip_cmd)
            send_registration_request_udp(
                grd_ipv6=grd_ipv6,
                grd_port=grd_port,
                user_ipv6=local_ipv6,
                callback_port=callback_port_runtime,
                init_sat_ipv6=init_sat_ipv6,
            )
            current_dev = init_dev
            start_registration_timeout()
            # For this example, we just log the registration action.
            logging.info(f"✉️ Sent registration request via {init_dev} to {grd_id}.")
        except Exception as e:
            logging.error(f"❌ Failed to send registration request: {e}")
            status = "not_registered"
            current_dev = None
    else:
        logging.warning("⚠️ No suitable access link found for registration.")

def lifetime_strategy(metadata: dict) -> Tuple[str, bool]:
    # Example handover strategy: always prefer the link with greatest ttl
    threshold_s = metadata.get("threshold_s", link_duration_initial_value_s/4.0)  # threshold for minimum remaining duration to consider a handover
    # compute remaining duration for available links and select the one with the longest remaining duration above threshold
    if current_dev == None:
        # no link currently assigned to user, so handover is needed to assign the best available link
        remaining_duration = 0
    elif links_db.get(current_dev,{}).get("status",None) != "available":
        # current link is not available, so handover is needed to assign the best available link
        remaining_duration = 0
    else:
        remaining_duration = links_db.get(current_dev, {}).get("last_duration", 0) - (time.time() - links_db.get(current_dev, {}).get("last_created", 0))
    
    if remaining_duration > threshold_s:
        # current link has enough remaining duration, no handover needed
        return current_dev, False
    
    available_devs = [(dev,l) for dev,l in links_db.items() if l.get("status") == "available"]
    if not available_devs:
        return "", False

    candidate_dev = max(available_devs, key=lambda x: x[1].get("last_duration", 0) - (time.time() - x[1].get("last_created", 0)))
    if candidate_dev[0] != current_dev:
        return candidate_dev[0],True
    else:
        return candidate_dev[0],False

def processing_handover_loop() -> None:
    while True:
        if status != "registered":
            time.sleep(handover_periodic_check_s)
            continue
        candidate_dev, local_handover_needed = is_handover_needed(handover_metadata)
        if local_handover_needed:
            logging.info(f"🔀 Handover decision selected newest dev {candidate_dev}")
            handle_handover(candidate_dev)
        time.sleep(handover_periodic_check_s)  # periodic check interval for handover decision (can be tuned based on expected link dynamics and handover time requirements)


# ----------------------------
#   WATCHERS
# ----------------------------
def watch_link_actions_loop (etcd_client) -> None:
    global status, current_dev, new_dev
    logging.info("👀 Watching /config/links (Dynamic Events)...")
    backoff = 1
    while True:
        cancel = None
        try:
            events_iterator, cancel = etcd_client.watch_prefix(KEY_LINKS_PREFIX)
            for event in events_iterator:
                if isinstance(event, etcd3.events.PutEvent):
                    # update link_db
                    l = json.loads(event.value.decode())
                    link_dev = event.key.decode().split("/")[-1]
                    if link_dev not in links_db:
                        remote_endpoint = l.get("endpoint1") if l.get("endpoint2") == node_name else l.get("endpoint2")
                        remote_endpoint_ipv6 = resolve_ipv6_from_hosts(remote_endpoint) if remote_endpoint else ""
                        logging.info(f"➕ Detected new link {link_dev} with data {l}")
                        update_link_db(link_dev=link_dev, etcd_link_data=l, last_created=time.time(), last_updated=time.time(), status="available", remote_endpoint_ipv6=remote_endpoint_ipv6)
                    elif links_db[link_dev].get("status") == "available":
                            logging.info(f"🔄 Detected update for existing link {link_dev}")
                            update_link_db(link_dev=link_dev, etcd_link_data=l, last_updated=time.time(), status="available")
                    elif links_db[link_dev].get("status") == "unavailable":
                            logging.info(f"🔁 Detected re-appearance of previously link {link_dev}, updating status to available")
                            update_link_db(link_dev=link_dev, etcd_link_data=l, last_created=time.time(), last_updated=time.time(), status="available")
                    if status == "not_registered":
                        handle_registration_request()

                elif isinstance(event, etcd3.events.DeleteEvent):
                    # update link_db
                    deleted_dev = event.key.decode().split("/")[-1]
                    last_duration = time.time() - links_db.get(deleted_dev, {}).get("last_created", time.time())
                    update_link_db(link_dev=deleted_dev, last_updated=time.time(), status="unavailable", last_duration=last_duration)
                    if deleted_dev == current_dev:
                        logging.warning(
                            "🛑 Current link %s deleted, resetting state and re-registering.",
                            deleted_dev,
                        )
                        status = "not_registered"
                        current_dev = None
                        new_dev = None
                        if handover_timeout_timer is not None:
                            handover_timeout_timer.cancel()
                        if registration_timeout_timer is not None:
                            registration_timeout_timer.cancel()
                        handle_registration_request()
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

# def is_handover_eligible_delay(current_iface,new_iface) -> bool:
#     # Example eligibility check: only trigger handover if delay difference is > 5ms
#     if current_iface is None:
#         return True
#     new_link_delay_ms = parse_delay(links_db.get(new_iface, {}).get("delay", float('inf')))
#     current_link_delay_ms = parse_delay(links_db.get(current_iface, {}).get("delay", float('inf')))
#     delay_diff = new_link_delay_ms - current_link_delay_ms
#     return delay_diff < -5  # Trigger handover if new link is at least 5ms better

# def is_handover_eligible_lifetime(current_iface,new_iface) -> bool:
#     ttl_current = links_db.get(current_iface, {}).get("last_duration", float('inf')) - (time.time() - links_db.get(current_iface, {}).get("last_created", 0))
#     ttl_new = links_db.get(new_iface, {}).get("last_duration", float('inf')) - (time.time() - links_db.get(new_iface, {}).get("last_created", 0))
#     return ttl_new > ttl_current  # Eligible if new link has longer expected lifetime than current


def handle_handover(candidate_dev: str) -> None:
    global status, current_dev, new_dev
    try:
        status = "handover_in_progress"
        candidate_sat = candidate_dev.split("_")[1]  # Assuming link_dev format is "vl_{remote_endpoint}_1"
        candidate_sat_ipv6 = resolve_ipv6_from_hosts(candidate_sat)
        if not wait_for_link_local_via_route(candidate_sat_ipv6, timeout_s=link_setup_delay_s):
            logging.warning(
                f"⚠️ No route with link-local next-hop for {candidate_sat_ipv6} before handover request timeout window."
            )
        send_handover_request_udp(
            grd_ipv6=grd_ipv6,
            port=grd_port,
            user_ipv6=local_ipv6,
            callback_port=callback_port_runtime,
            new_sat_ipv6=candidate_sat_ipv6,
        )
        logging.info(f"✉️ Sent handover request for new sat {candidate_sat} to {grd_id}.")
        start_handover_command_timeout(timeout_s=handover_command_timeout_s)
        new_dev = candidate_dev
    except Exception as ex:
        logging.error("❌ Failed to send handover request: %s", ex)
        return

def handle_handover_command(payload: Dict[str, Any], ho_delay_ms: float) -> None:
    global status, current_dev, new_dev

    if status != "handover_in_progress" and payload.get("type") != "handover_command_unsolicited":
        logging.warning("⚠️ Received handover_command while not in handover_in_progress state, ignoring.")
        return

    grd_id_recv = payload["grd_id"]
    if grd_id_recv != grd_id:
        logging.warning(f"⚠️ Received handover_command for grd_id {grd_id_recv} while current grd_id is {grd_id}, ignoring.")
        return                  
    upstream_sids = payload["sids"]                  # new sid sequence for the user (e.g., "2001:db8:200::1")
    new_sat_ipv6_recv = upstream_sids.split(",")[0]          # first SID is the new sat to reach the grd.
    if status == "handover_in_progress":
        route_dev = new_dev
        new_sat_ipv6 = links_db.get(new_dev, {}).get("remote_endpoint_ipv6", "")
    else:
        # unsolicited handover should not change current dev
        route_dev = current_dev
        new_sat_ipv6 = links_db.get(current_dev, {}).get("remote_endpoint_ipv6", "")
    
    if new_sat_ipv6_recv != new_sat_ipv6:
        type_str = "unsolicited handover_command" if payload.get("type") == "handover_command_unsolicited" else "handover_command"
        logging.warning(f"⚠️ Received {type_str} with new sat {new_sat_ipv6_recv} different from expected {new_sat_ipv6}, rejecting.")
        return
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
    
    # add new route to grd
    if not wait_for_link_local_via_route(new_sat_ipv6, timeout_s=link_setup_delay_s):
        logging.warning(
            f"⚠️ No route with link-local next-hop for {new_sat_ipv6} before handover command timeout window."
        )
    ip_cmd = build_srv6_route_replace(grd_ipv6, new_sat_ipv6, route_dev)
    run_cmd(ip_cmd)
    # add new default route
    default_prefix = "default"
    ip_cmd = build_srv6_route_replace(default_prefix, upstream_sids, route_dev)
    run_cmd(ip_cmd)
    if payload.get("type") == "handover_command":
        current_dev = new_dev
        new_dev = None
    status = "registered" if payload.get("type") == "handover_command" else status
    cancel_handover_command_timeout() if payload.get("type") == "handover_command" else None
    if payload.get("type") == "handover_command":
        logging.info(f"📡 Handover accepted by {grd_id} with upstream SIDs {upstream_sids} via new dev {current_dev}")
    elif payload.get("type") == "handover_command_unsolicited":
        logging.info(f"📡 Unsolicited handover command received for {grd_id} with upstream SIDs {upstream_sids} via dev {current_dev}")

def handle_registration_accept(payload: Dict[str, Any]) -> None:
    global status, current_dev

    if status != "registration_in_progress":
        logging.debug("⚠️ Received registration_accept while not in registration_in_progress state, ignoring.")
        return

    grd_id_recv = payload["grd_id"]                      
    upstream_sids = payload["sids"]                  # new sid sequence for the user (e.g., "2001:db8:200::1")
    init_sat_ipv6_recv = upstream_sids.split(",")[0]         # first SID is the new egress SID to reach the grd. Shall be the IP address of a connected sat
    
    init_sat_ipv6 = links_db.get(current_dev, {}).get("remote_endpoint_ipv6", "")
    if grd_id_recv != grd_id:
        logging.warning(f"⚠️ Received registration_accept from {grd_id_recv} while current grd is {grd_id}, ignoring.")
        return
    if init_sat_ipv6_recv != init_sat_ipv6:
        logging.warning(f"⚠️ Received registration_accept with init sat {init_sat_ipv6_recv} different from expected {init_sat_ipv6}, ignoring.")
        return

    # add ipv6 default route via grd_ipv6
    default_prefix = "default"
    ip_cmd = build_srv6_route_replace(default_prefix, upstream_sids, current_dev)
    run_cmd(ip_cmd)
    cancel_registration_timeout()
    status = "registered"
    logging.info(f"📡 Registration accepted by {grd_id} with with upstream SIDs {upstream_sids} via dev {current_dev}")
    
def handle_command(payload: Dict[str, Any], ho_delay_ms: float, grd_id: int) -> None:
    if payload.get("type") == "handover_command":
        handle_handover_command(payload, ho_delay_ms)
        return
    elif payload.get("type") == "handover_command_unsolicited":
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
                    prepare_qdisc_for_grd(grd_ipv6=msg.get("grd_ipv6"), grd_id=grd_id)
            handle_command(msg, ho_delay_ms=ho_delay, grd_id=grd_id)
        except Exception as e:
            logging.warning("❌ Failed command from [%s]:%d: %s", peer[0], peer[1], e)


def main() -> None:
    global is_handover_needed, grd_ipv6, grd_port, grd_id, callback_port_runtime, local_ipv6, etcd_client_runtime, link_setup_delay_s, handover_metadata, registration_accept_timeout_s, handover_command_timeout_s, link_duration_initial_value_s
    
    ap = argparse.ArgumentParser()
    ap.add_argument("--bind", default="::")
    ap.add_argument("--port", type=int, default=5006, help="UDP port where usr1 listens for handover_command")
    ap.add_argument("--ho-delay", type=float, help="Handover delay in mseconds (requires veth0_rt, use app/shaping-ns-create-v6.sh)", default=0)
    ap.add_argument("--grd", required=True, help="IPv6 address of the serving ground station or name resolvable via /etc/hosts (e.g., grd1 or 2001:db8:101::1)")
    ap.add_argument("--grd-port", type=int, default=5005, help="UDP port on serving ground station (default: 5005)")
    ap.add_argument("--registration-timeout", type=float, default=3.0, help="seconds to wait for registration_accept before retrying registration")
    ap.add_argument("--handover-timeout", type=float, default=3.0, help="seconds to wait for handover_command before considering handover failed and reverting to registered state")
    ap.add_argument("--handover-strategy", choices=["lifetime"], default="lifetime", help="handover eligibility strategy (default: lifetime)")
    ap.add_argument("--handover-strategy-metadata", type=json.loads, default='{}', help="JSON string with metadata parameters for the handover strategy (e.g., threshold values, weights, etc.)")  
    ap.add_argument("--link-setup-delay", type=float, default=3, help="Estimated time in seconds needed by to setup relevat routes and interfaces after link creatio, default 5s)")
    ap.add_argument("--link-duration-initial-value", type=float, default=4*60, help="Initial value in seconds for the duration of new links, default: 4min)")
    ap.add_argument("--no-auto", action="store_true", help="disable automatic handover and registration")
    ap.add_argument("--log-level", default="INFO", help="Logging level (e.g., DEBUG, INFO, WARNING)")
    args = ap.parse_args()
    
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(message)s")
    refresh_hosts_ipv6_cache()
    
    local_ipv6 = resolve_ipv6_from_hosts(os.environ["NODE_NAME"])
    # resolve grd_address if it's a hostname
    grd_ipv6 = args.grd
    if not ":" in args.grd:  # crude check for hostname vs IPv6
        try:
            grd_ipv6 = resolve_ipv6_from_hosts(args.grd)
            logging.debug("Resolved ground station address %s to %s", args.grd, grd_ipv6)
        except Exception as e:
            logging.error(f"Failed to resolve ground station address {args.grd}: {e}")
            sys.exit(1)
    
    grd_id = args.grd
    grd_port = args.grd_port
    callback_port_runtime = args.port
    registration_accept_timeout_s = args.registration_timeout
    handover_command_timeout_s = args.handover_timeout
    handover_metadata = args.handover_strategy_metadata
    etcd_client=get_etcd_client()
    etcd_client_runtime = etcd_client
    link_setup_delay_s = args.link_setup_delay
    link_duration_initial_value_s = args.link_duration_initial_value
    
    if args.handover_strategy == "lifetime":
        is_handover_needed = lifetime_strategy
    else:
        logging.error(f"Unsupported handover strategy: {args.handover_strategy}")
        sys.exit(1)

    preload_links_db_from_etcd(etcd_client)
    
    if args.no_auto:
        logging.info("🚫 Auto handover and registration disabled, skipping initial registration.")
    else:
        handle_registration_request()

    if not args.no_auto:
        threading.Thread(
            target=watch_link_actions_loop,
            args=(etcd_client,),
            daemon=True,
            name="watch-link-actions",
        ).start()

        threading.Thread(
            target=processing_handover_loop,
            daemon=True,
            name="processing-handover",
        ).start()
    
    serve(bind_addr=args.bind, port=args.port, ho_delay=args.ho_delay)

if __name__ == "__main__":
    main()
