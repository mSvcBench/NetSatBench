#!/usr/bin/env python3
import argparse
from email.policy import default
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
link_duration_initial_value_s = 4*60  # initial value for link duration (sec)
max_links = 16  # max number of simultaneous links (can be tuned based on expected number of available links and resource constraints)
is_user_handover_needed = None  # assign the handover strategy function to use for processing user handover decisions
is_grd_handover_needed = None  # assign the handover strategy function to use for processing grd handover decisions
process_connection_handover = None  # assign the function to process the set of links/devs to be used for connecting to the satellite network
handover_metadata = {}  # metadata dict to pass to the handover strategy function (can include threshold values, weights, or other parameters needed for the strategy logic)
handover_periodic_check_s = 3.3  # periodic check interval for handover decision (can be tuned based on expected link dynamics and handover time requirements)
handover_delay_ms = 0.0
hosts_ipv6_cache: Dict[str, str] = {}
grd_ipv6 = ""
user_callback_port = 5006
heartbeat_interval_s = 1.0
heartbeat_max_failures = 3
heartbeat_failures: Dict[str, int] = {}
heartbeat_lock = threading.Lock()
_UNSET = object() # sentinel value to distinguish between "no update" and "update with None/empty" in db update functions
MAX_UDP_RECV_BYTES = 65535 # max size of UDP payload to receive for user callbacks, can be tuned based on expected message size and memory constraints
report_file = None  # file handle for detailed report output, if enabled by args

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


def update_user_db(user_id: str, user_ipv6: Any = _UNSET, upstream_sids: Any = _UNSET, downstream_sids: Any = _UNSET, grd_dev: Any = _UNSET, status: Any = _UNSET, user_links_db: Any = _UNSET, txid: Any = _UNSET, user_dev: Any = _UNSET, qdisc_minor: Any = _UNSET) -> None:
    global user_db
    user_db[user_id] = {
        "user_ipv6": user_ipv6 if user_ipv6 is not _UNSET else user_db.get(user_id, {}).get("user_ipv6", None),
        "user_dev": user_dev if user_dev is not _UNSET else user_db.get(user_id, {}).get("user_dev", None),
        "upstream_sids": upstream_sids if upstream_sids is not _UNSET else user_db.get(user_id, {}).get("upstream_sids", None),
        "downstream_sids": downstream_sids if downstream_sids is not _UNSET else user_db.get(user_id, {}).get("downstream_sids", None),
        "grd_dev": grd_dev if grd_dev is not _UNSET else user_db.get(user_id, {}).get("grd_dev", None),
        "status": status if status is not _UNSET else user_db.get(user_id, {}).get("status", None),
        "user_links_db": user_links_db if user_links_db is not _UNSET else user_db.get(user_id, {}).get("user_links_db", None),
        "txid": txid if txid is not _UNSET else user_db.get(user_id, {}).get("txid", None),
        "qdisc_minor": qdisc_minor if qdisc_minor is not _UNSET else user_db.get(user_id, {}).get("qdisc_minor", None),
    }
    if user_id == node_name:
        user_db[user_id]["status"] = "registered"  # self user is always considered registered once we have its info in the db

def update_link_db(link_dev: str, etcd_link_data: Any = _UNSET, last_created: Any = _UNSET, last_updated: Any = _UNSET, status: Any = _UNSET, last_duration: Any = _UNSET, remote_endpoint_ipv6: Any = _UNSET) -> None:
    global links_db
    links_db.setdefault(link_dev, {})
    if etcd_link_data is not _UNSET and etcd_link_data is not None:
        for key in etcd_link_data:
            links_db[link_dev][key] = etcd_link_data[key]
        links_db[link_dev]["remote_endpoint_name"] = etcd_link_data.get("endpoint2") if etcd_link_data.get("endpoint1") == node_name else etcd_link_data.get("endpoint1")

    links_db[link_dev]["last_created"] = last_created if last_created is not _UNSET else links_db.get(link_dev, {}).get("last_created", None)
    links_db[link_dev]["last_updated"] = last_updated if last_updated is not _UNSET else links_db.get(link_dev, {}).get("last_updated", None)
    links_db[link_dev]["status"] = status if status is not _UNSET else links_db.get(link_dev, {}).get("status", None)
    links_db[link_dev]["last_duration"] = last_duration if last_duration is not _UNSET else links_db.get(link_dev, {}).get("last_duration", link_duration_initial_value_s)
    links_db[link_dev]["remote_endpoint_ipv6"] = remote_endpoint_ipv6 if remote_endpoint_ipv6 is not _UNSET else links_db.get(link_dev, {}).get("remote_endpoint_ipv6", None)


def get_user_index(user_id: str) -> int:
    try:
        return list(user_db.keys()).index(user_id)
    except ValueError as e:
        raise RuntimeError(f"User '{user_id}' not tracked in user_db") from e


def get_user_qdisc_minor(user_id: str) -> int:
    minor = user_db.get(user_id, {}).get("qdisc_minor")
    if minor is None:
        raise RuntimeError(f"User '{user_id}' has no qdisc minor assigned")
    return minor


def allocate_user_qdisc_minor() -> int | None:
    used_minors = {
        user_info.get("qdisc_minor")
        for user_info in user_db.values()
        if user_info.get("qdisc_minor") is not None
    }
    for minor in range(10, 65536):
        if minor not in used_minors:
            return minor
    return None


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


def parse_delay(delay) -> float:
    if isinstance(delay, (int, float)):
        return float(delay)
    if isinstance(delay, str):
        delay = delay.strip().lower()
        if delay.endswith("ms"):
            return float(delay[:-2])
        if delay.endswith("us"):
            return float(delay[:-2]) / 1000
        if delay.endswith("s"):
            return float(delay[:-1]) * 1000
        raise ValueError(f"Unknown delay format: {delay}")
    raise ValueError(f"Invalid delay type: {type(delay)}")


def parse_expected_duration(expected_duration: Any) -> float:
    if expected_duration is None:
        return float(link_duration_initial_value_s)
    if isinstance(expected_duration, (int, float)):
        return float(expected_duration)
    if isinstance(expected_duration, str):
        value = expected_duration.strip().lower()
        if value == "null" or value == "":
            return float(link_duration_initial_value_s)
        if value.endswith(("ms", "us", "s")):
            delay_ms = parse_delay(value)
            return delay_ms / 1000.0
        return float(value)
    raise ValueError(f"Invalid expected_duration type: {type(expected_duration)}")

# ----------------------------
#   MAIN LOGIC FOR LINK MANAGEMENT LOCAL SIDE
# ----------------------------
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
                remote_endpoint = ep2 if ep1 == node_name else ep1
                remote_endpoint_ipv6 = resolve_ipv6_from_hosts(remote_endpoint) if remote_endpoint else None
                update_link_db(link_dev=link_dev, etcd_link_data=l, last_created=time.time(), last_updated=time.time(), status="available", last_duration=link_duration_initial_value_s, remote_endpoint_ipv6=remote_endpoint_ipv6)
                loaded += 1
            except Exception as e:
                skipped += 1
                logging.error("❌ Skipping malformed initial link entry: %s", e)
        logging.info("📥 Initial links preload completed: loaded=%d skipped=%d", loaded, skipped)
    except Exception as e:
        logging.error("❌ Failed to preload initial links from Etcd: %s", e)

def handle_link_put_action(event):
    ## Process PutEvent (Add/Update)
    if not isinstance(event, etcd3.events.PutEvent):
        logging.error("❌ Ignoring non-PutEvent for handover processing.")
        return
    try:    
        link_dev = event.key.decode().split("/")[-1]  # Assuming key format includes dev_id at the end
        l = json.loads(event.value.decode())

        # Check if this is an update of available links
        if link_dev in links_db and (links_db[link_dev].get("status") == "available" or links_db[link_dev].get("status") == "connected"):
            remote_endpoint = links_db[link_dev].get("remote_endpoint_name", "unknown")
            logging.info(f"🔄 Ground station detected update for link with satellite {remote_endpoint}")
            update_link_db(link_dev=link_dev, etcd_link_data=l, last_updated=time.time())
        else:
            ep1, ep2 = l.get("endpoint1"), l.get("endpoint2")
            remote_endpoint = ep2 if ep1 == node_name else ep1
            remote_endpoint_ipv6 = resolve_ipv6_from_hosts(remote_endpoint) if remote_endpoint else None
            logging.info(f"➕ Ground station detected satellite {remote_endpoint} in range") 
            update_link_db(link_dev=link_dev, etcd_link_data=l, last_created=time.time(), last_updated=time.time(), status="available", last_duration=link_duration_initial_value_s, remote_endpoint_ipv6=remote_endpoint_ipv6)
        if "expected_duration" in l:
            # expected_duration is expressed in seconds by the epoch annotation tool; null falls back to the default initial duration.
            expected_duration = parse_expected_duration(l["expected_duration"])
            update_link_db(link_dev=link_dev, last_duration=expected_duration)
        return
    except Exception as ex:
        logging.error("❌ Failed to process link action event %s", ex)

def handle_link_delete_action(event):
    ## Process DeleteEvent (Remove)
    if not isinstance(event, etcd3.events.DeleteEvent):
        logging.error("❌ Ignoring non-DeleteEvent for handover processing.")
        return
    try:
        link_dev = event.key.decode().split("/")[-1]  # Assuming key format includes dev_id at the end
        if link_dev in links_db:
            remote_endpoint = links_db[link_dev].get("remote_endpoint_name", "unknown")
            logging.info(f"➖ Ground station detected satellite {remote_endpoint} out of range")
            update_link_db(link_dev=link_dev, status="unavailable", last_duration=time.time() - links_db[link_dev].get("last_created", time.time()))
        else:
            logging.error(f"❌ Received delete event for unknown link device {link_dev}, ignoring.")
            return
    except Exception as ex:
        logging.error("❌ Failed to process link delete event %s", ex)
        return
    # Evaluate handover decision if any user was using the link that just got deleted
    for user_id, user_info in user_db.items():
        if user_info.get("grd_dev") == link_dev:
            sat_name = links_db.get(link_dev, {}).get("remote_endpoint_name", "unknown")
            logging.info(f"⚠️ Ground station using out of range satellite {sat_name} for user {user_id}, evaluating handover decision...")
            update_user_db(user_id=user_id, grd_dev=None)  # Update user_db with new status for the user during local handover processing
            new_grd_dev, needed = is_grd_handover_needed(user_id, handover_metadata)
            if needed:
                sat_name = links_db.get(new_grd_dev, {}).get("remote_endpoint_name", "unknown")
                process_user_handover(user_id, new_grd_dev)
            else:
                logging.warning(f"⚠️ No available link found for {user_id} after deletion of {sat_name}, {user_id} is now without a grd link")

def lifetime_strategy_user_handover(user_id: str, metadata: dict) -> Tuple[str, bool]:
    threshold_s = metadata.get("threshold_s", link_duration_initial_value_s/4.0)  # threshold for minimum remaining duration to consider a handover
    current_dev = user_db.get(user_id, {}).get("user_dev", None)
    current_links_db = user_db.get(user_id, {}).get("user_links_db", {})
    status_value = "available"  # for user dev we consider all available links as candidates for handover
    
    if current_dev == None:
        # no link currently assigned to user, so handover is needed to assign the best connected link
        remaining_duration = 0
    elif current_links_db.get(current_dev,{}).get("status",None) != status_value:
            # current link is no more connected (grd) or available (user), so handover is needed to assign the best connected link
        remaining_duration = 0
    else:
        remaining_duration = current_links_db.get(current_dev, {}).get("last_duration", 0) - (time.time() - current_links_db.get(current_dev, {}).get("last_created", 0))
    
    if remaining_duration > threshold_s:
        # current link has enough remaining duration, no handover needed
        return current_dev, False
    
    candidate_devs = [(dev,l) for dev,l in current_links_db.items() if l.get("status") == status_value]
    if not candidate_devs:
        return "", False

    candidate_dev = max(candidate_devs, key=lambda x: x[1].get("last_duration", 0) - (time.time() - x[1].get("last_created", 0)))
    if candidate_dev[0] != current_dev:
        return candidate_dev[0],True
    else:
        return candidate_dev[0],False

def lifetime_strategy_grd_handover(user_id: str, metadata: dict) -> Tuple[str, bool]:
    # Example handover strategy: always prefer the link with greatest ttl
    threshold_s = metadata.get("threshold_s", link_duration_initial_value_s/4.0)  # threshold for minimum remaining duration to consider a handover
    current_dev = user_db.get(user_id, {}).get("grd_dev", None)
    current_links_db = links_db
    status_value = "connected" # for grd dev we consider only connected links as candidates for handover
    
    if current_dev == None:
        # no link currently assigned to user, so handover is needed to assign the best connected link
        remaining_duration = 0
    elif current_links_db.get(current_dev,{}).get("status",None) != status_value:
            # current link is no more connected (grd) or available (user), so handover is needed to assign the best connected link
        remaining_duration = 0
    else:
        remaining_duration = current_links_db.get(current_dev, {}).get("last_duration", 0) - (time.time() - current_links_db.get(current_dev, {}).get("last_created", 0))
    
    if remaining_duration > threshold_s:
        # current link has enough remaining duration, no handover needed
        return current_dev, False
    
    candidate_devs = [(dev,l) for dev,l in current_links_db.items() if l.get("status") == status_value]
    if not candidate_devs:
        return "", False

    candidate_dev = max(candidate_devs, key=lambda x: x[1].get("last_duration", 0) - (time.time() - x[1].get("last_created", 0)))
    if candidate_dev[0] != current_dev:
        return candidate_dev[0],True
    else:
        return candidate_dev[0],False
    
def lifetime_strategy_connection_handover(metadata):
    # update of the connectted satellite links that can be used by users 
    max_dev = metadata.get("max_links", max_links)  # max number of simultaneous links (can be tuned based on expected number of available links and resource constraints)
    threshold_s = metadata.get("threshold_s", link_duration_initial_value_s/4.0)  # threshold for minimum remaining duration to consider a handover
    # compute remaining duration for available links and select the one with the longest remaining duration above threshold
    current_links = [(dev,l) for dev,l in links_db.items() if l.get("status") == "connected"]
    available_links = [(dev,l) for dev,l in links_db.items() if l.get("status") == "available"]
    sorted_available_links = sorted(available_links, key=lambda x: x[1].get("last_duration", 0) - (time.time() - x[1].get("last_created", 0)), reverse=True)

    # fill connection slots
    current_links_count = len(current_links)
    current_available_links_count = len(sorted_available_links)
    for i in range(min(max_dev - current_links_count, current_available_links_count)):
        link_to_connect = sorted_available_links.pop(0)[0]
        update_link_db(link_dev=link_to_connect, status="connected")
        sat_name = links_db.get(link_to_connect, {}).get("remote_endpoint_name", "unknown")
        logging.info(f"✅ Ground station connected with satellite {sat_name} with remaining duration {(links_db.get(link_to_connect, {}).get('last_duration', 0) - (time.time() - links_db.get(link_to_connect, {}).get('last_created', 0))):.1f}s to fill available connection slots")
    
    link_switch={}
    for cl in current_links:
        remaining_duration = cl[1].get("last_duration", 0) - (time.time() - cl[1].get("last_created", 0))
        if remaining_duration <= threshold_s:
            # current link has low remaining duration, consider it for handover
            if sorted_available_links:
                candidate_remaining_duration = sorted_available_links[0][1].get("last_duration", 0) - (time.time() - sorted_available_links[0][1].get("last_created", 0))
                if candidate_remaining_duration > remaining_duration:
                    link_switch[cl[0]] = sorted_available_links[0][0]  # select the available link with the longest remaining duration as candidate for handover
                    sorted_available_links.pop(0)  # remove the selected link from the available list for the next iterations

    # apply link switch decisions
    for old_dev, new_dev in link_switch.items():
        update_link_db(link_dev=old_dev, status="available")
        update_link_db(link_dev=new_dev, status="connected")
        old_sat_name = links_db.get(old_dev, {}).get("remote_endpoint_name", "unknown")
        new_sat_name = links_db.get(new_dev, {}).get("remote_endpoint_name", "unknown")
        logging.info(f"🔄 Ground station switched connection from satellite {old_sat_name} to satellite {new_sat_name} due to low remaining duration on old sat")


def processing_handover_loop() -> None:
    while True:
        process_connection_handover(handover_metadata)  # evaluate if we need to switch some of the connected devs to new ones based on the connection handover strategy
        for user_id in list(user_db.keys()):
            if user_db[user_id].get("status") != "registered":
                logging.warning(f"⚠️ Skipping handover processing for user {user_id} which is not in registered state")
                continue
            new_grd_dev, grd_needed = is_grd_handover_needed(user_id, handover_metadata)
            if user_id != node_name:
                new_user_dev, user_needed = is_user_handover_needed(user_id, handover_metadata)
            else:
                new_user_dev, user_needed = None, False  # self user doesn't have a satellite dev, so we skip user handover evaluation for it
            if grd_needed or user_needed:
                strategy_type = "grd" if grd_needed and not user_needed else "user" if user_needed and not grd_needed else "grd+user"
                new_grd_sat_name = links_db.get(new_grd_dev, {}).get("remote_endpoint_name", "unknown")
                if new_user_dev is not None:
                    new_user_sat_name = user_db.get(user_id, {}).get("user_links_db", {}).get(new_user_dev, {}).get("remote_endpoint_name", "unknown")
                    logging.info(f"🔀 Handover type '{strategy_type}' for user {user_id}: selected newest grd satellite {new_grd_sat_name} and user satellite {new_user_sat_name}")
                else:
                    logging.info(f"🔀 Handover type '{strategy_type}' for {user_id}: selected newest default satellite {new_grd_sat_name}")
                
                process_user_handover(user_id, new_grd_dev, new_user_dev)
                if report_file:
                    report_file.write(f"{time.time()},handover,{user_id},{strategy_type},{new_grd_sat_name},{new_user_sat_name}\n")
                    report_file.flush()

                if user_needed and handover_delay_ms > 0: 
                    threading.Thread(
                            target=traffic_pause,
                            args=(user_id, handover_delay_ms),
                            daemon=True,
                            name=f"traffic-pause-{user_id}",
                        ).start()
        
        time.sleep(handover_periodic_check_s)  # periodic check interval for handover decision 

def send_user_hello_udp(user_id: str, user_ipv6: str) -> None:
    msg: Dict[str, Any] = {
        "type": "hello",
        "grd_id": os.environ["NODE_NAME"],
        "user_id": user_id,
        "txid": str(int(time.time() * 1000)),
    }
    with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as hb_sock:
        send_udp_json(hb_sock, msg, (user_ipv6, user_callback_port, 0, 0))

def heartbeat_monitor_loop() -> None:
    while True:
        for user_id, user_info in list(user_db.items()):
            if user_id == node_name:
                continue
            if user_info.get("status") != "registered":
                continue
            user_ipv6 = user_info.get("user_ipv6", "")
            if not user_ipv6:
                continue
            try:
                send_user_hello_udp(user_id=user_id, user_ipv6=user_ipv6)
            except Exception as e:
                logging.warning("⚠️ Failed sending heartbeat HELLO to user %s: %s", user_id, e)
            with heartbeat_lock:
                misses = heartbeat_failures.get(user_id, 0) + 1
                heartbeat_failures[user_id] = misses
            if misses >= heartbeat_max_failures:
                user_ipv6 = user_info.get("user_ipv6", "unknown")
                if user_ipv6 != "unknown":
                    # remove user route before removing user from db
                    try:
                        ip_cmd = ["ip", "-6", "route", "del", user_ipv6, "dev", user_info.get("grd_dev", "unknown")]
                        run_cmd(ip_cmd)
                    except Exception as e:
                        logging.error("❌ Failed to remove route for user %s with IPv6 %s after missed heartbeats: %s", user_id, user_ipv6, e)
                try:
                    remove_qdisc_for_user(user_ipv6=user_info.get("user_ipv6", ""), user_id=user_id)
                except Exception as e:
                    logging.error("❌ Failed to remove qdisc state for user %s after missed heartbeats: %s", user_id, e)
                user_db.pop(user_id, None)
                with heartbeat_lock:
                    heartbeat_failures.pop(user_id, None)
                logging.warning("⚠️ Removed user %s from user_db after %d missed heartbeat", user_id, misses)
        time.sleep(heartbeat_interval_s)


def create_sids(grd_sat_ipv6: str, user_sat_ipv6: str) -> Tuple[str, str]:
    # Build downstream SID list without empty values to avoid generating "::" entries.
    downstream_sid_list: List[str] = []
    if grd_sat_ipv6:
        downstream_sid_list.append(grd_sat_ipv6)
    if user_sat_ipv6 and user_sat_ipv6 != grd_sat_ipv6:
        downstream_sid_list.append(user_sat_ipv6)
    downstream_sids = ",".join(downstream_sid_list)

    # Upstream is reverse path of downstream plus local GRD endpoint.
    upstream_sid_list = list(reversed(downstream_sid_list))
    if grd_ipv6:
        upstream_sid_list.append(grd_ipv6)
    upstream_sids = ",".join(upstream_sid_list)
    return downstream_sids, upstream_sids

def process_user_handover(user_id ,new_grd_dev = None, new_user_dev=None) -> None:
    # reroute user traffic to new link 
    user = user_db.get(user_id)
    if user.get("status") != "registered":
        logging.warning(f"⚠️ Attempted local handover for user {user_id} which is not in registered state, skipping")
        return
    # update_user_db(user_id=user_id, status="handover_in_progress")  # Update user_db with new status for the user during local handover processing
    user_ipv6 = user.get("user_ipv6", "")
    
    # new values for handover
    grd_sat_ipv6 = links_db.get(new_grd_dev, {}).get("remote_endpoint_ipv6", "")
    user_sat_ipv6 = user.get("user_links_db", {}).get(new_user_dev, {}).get("remote_endpoint_ipv6", "") if new_user_dev else ""

    if not grd_sat_ipv6:
        logging.error(f"❌ No remote endpoint IPv6 found for new GRD dev {new_grd_dev} during handover of user {user_id}, aborting handover")
        #update_user_db(user_id=user_id, status="registered")  # revert user status to registered since handover cannot proceed
        return
    if new_user_dev and not user_sat_ipv6:
        logging.error(f"❌ No remote endpoint IPv6 found for new USER dev {new_user_dev} during handover of user {user_id}, aborting handover")
        #update_user_db(user_id=user_id, status="registered")  # revert user status to registered since handover cannot proceed
        return

    # compute sids
    new_downstream_sids, new_upstream_sids = create_sids(grd_sat_ipv6, user_sat_ipv6)
    try:
        # update user db 
        if user_id == node_name:
            # when user id == node_name we are doing an handover for routing the satellite subnet
            # no need to wait confirm. Apply new route immediately
            if not wait_for_link_local_via_route(grd_sat_ipv6, timeout_s=link_setup_delay_s):
                logging.warning(
                    f"⚠️ No route with link-local next-hop for {grd_sat_ipv6} before local handover"
                )
            # inject new route
            ip_cmd = build_srv6_route_replace(dst_prefix=user_ipv6, sids=new_downstream_sids, dev=new_grd_dev, metric=20)
            run_cmd(ip_cmd)
            update_user_db(user_id=user_id, 
                            user_ipv6=user_ipv6, 
                            downstream_sids=new_downstream_sids, 
                            grd_dev=new_grd_dev,
                            user_dev = new_user_dev,
                            status="registered")
            grd_sat_name = links_db.get(new_grd_dev, {}).get("remote_endpoint_name", "unknown")
            logging.info(f"✅  Handover completed for ground station default link with satellite {grd_sat_name}")
            return  # skip sending handover command to self in case of local handover for the satellite subnet
        
        # send handover command to user to update upstream SIDs with new dev ipv6
        callback_port = user_callback_port
        txid = str(int(time.time() * 1000))
        cmd_msg = {
            "type": "handover_command",
            "txid": txid,
            "grd_id": os.environ["NODE_NAME"],
            "grd_ipv6": grd_ipv6,
            "sids": new_upstream_sids,
        }
        with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as notify_sock:
                send_udp_json(notify_sock, cmd_msg, (user_ipv6, callback_port, 0, 0))
        logging.info(f"✉️ Sent handover command to user {user_id} with sid={new_upstream_sids}")

    except Exception as e:
        logging.error(f"❌ Local handover failed for user {user_id} : {e}")
    return


# ----------------------------
#   COMMAND HANDLERS
# ----------------------------
def handle_user_registration_request(
    sock: socket.socket,
    payload: Dict[str, Any],
    peer: Tuple[str, int, int, int],
    ho_delay_ms: float
) -> None:


    # Apply route change on grd to steer traffic to usr via new satellite
    try:
        user_id = payload["user_id"]
        user_ipv6 = payload["user_ipv6"]        
        user_sat_dev = payload.get("init_sat_dev", "")
        user_links_db = payload.get("user_links_db", payload.get("user_links_db", ""))
        if isinstance(user_links_db, str):
            user_links_db = json.loads(user_links_db) if user_links_db else {}
    except KeyError as e:
        logging.error(f"❌ Invalid registration request payload: {e}")
        return

    logging.info(f"👤 Received registration request from {user_id}")
    user = user_db.get(user_id, None)
    if user is None:
        logging.info(f"👤 New user {user_id} registering for the first time")
        qdisc_minor = allocate_user_qdisc_minor()
        if qdisc_minor is None:
            logging.warning(f"⚠️ No qdisc classid available for user {user_id}, registration refused")
            return
        update_user_db(user_id=user_id, user_ipv6=user_ipv6, status="not-registered", qdisc_minor=qdisc_minor)  # initialize user_db entry for the new user
        prepare_qdisc_for_new_user(user_ipv6=user_ipv6, user_id=user_id)
        user = user_db.get(user_id)
    try:
        if user.get("status") == "registration_in_progress":
            logging.warning(f"⚠️ Registration already in progress for user {user_id}, ignoring duplicate registration request")
            return
        update_user_db(user_id=user_id, user_ipv6=user_ipv6, user_links_db=user_links_db, user_dev=user_sat_dev, status="registration_in_progress")
        
        # chose of the grd dev to serve the user    
        grd_dev, needed = is_grd_handover_needed(user_id, handover_metadata)
        grd_sat_ipv6 = links_db.get(grd_dev, {}).get("remote_endpoint_ipv6", "") if grd_dev else ""
        
        if grd_dev == "" or grd_sat_ipv6 == "":
            logging.warning(f"⚠️ No suitable access satellite found for user {user_id}, registration aborted")
            # reset user state
            update_user_db(user_id=user_id, user_ipv6=user_ipv6, upstream_sids= None, downstream_sids=None, grd_dev=None, user_links_db=None, user_dev = None, status="not-registered")
            return
        
        # chose the access user dev
        user_dev_new, _ = is_user_handover_needed(user_id, handover_metadata)
        user_sat_ipv6_new = user_links_db.get(user_dev_new, {}).get("remote_endpoint_ipv6", "") if user_dev_new else ""
        if user_dev_new == "" or user_sat_ipv6_new == "":
            logging.warning(f"⚠️ No suitable user satellite dev found for user {user_id}, registration aborted")
            # reset user state
            update_user_db(user_id=user_id, user_ipv6=user_ipv6, upstream_sids= None, downstream_sids=None, grd_dev=None, user_links_db=None, user_dev = None, status="not-registered")
            return
        grd_sat_name = links_db.get(grd_dev, {}).get("remote_endpoint_name", "unknown")
        user_sat_name = user_links_db.get(user_dev_new, {}).get("remote_endpoint_name", "unknown")
        logging.info(f"✅ Registration for user {user_id} accepted: grd satellite {grd_sat_name} and user satellite {user_sat_name}")

        if report_file:
            report_file.write(f"{time.time()},registration,{user_id},_,{grd_sat_name},{user_sat_name}\n")
            report_file.flush()
        # build sids
        downstream_sids, upstream_sids = create_sids(grd_sat_ipv6, user_sat_ipv6_new)

        # user route injection
        ip_cmd = build_srv6_route_replace(dst_prefix = user_ipv6, sids = downstream_sids, dev = grd_dev)
        run_cmd(ip_cmd)

        # Sending registration_accept to usr with the sids to use 
        callback_port = payload.get("callback_port", user_callback_port)  # Optional port to send registration_accept back to usr
        txid = payload.get("txid", str(int(time.time() * 1000))) # nonce txid for correlation (default: current timestamp in ms)
        cmd_msg = {
            "type": "registration_accept",
            "txid": txid,
            "grd_id": os.environ["NODE_NAME"],  
            "grd_ipv6": grd_ipv6,  # IPv6 usr should use to reach grd (e.g., "2001:db8:100::2/128")
            "sids": upstream_sids,  # SID usr must use to reach grd
        }
        peer_for_cmd = (peer[0], callback_port, peer[2], peer[3])  # keep flowinfo/scopeid
        send_udp_json(sock, cmd_msg, peer_for_cmd)
        logging.info(f"✉️ Sent registration accept to {user_id}")
        logging.info(f"✅ Registration completed for user {user_id} with upstream sid {upstream_sids} and downstream sid {downstream_sids}")

        update_user_db(
            user_id=user_id,
            user_ipv6=user_ipv6,
            user_dev=user_dev_new,
            upstream_sids=upstream_sids,
            downstream_sids=downstream_sids,
            grd_dev=grd_dev,
            status="registered"
        )
        with heartbeat_lock:
            heartbeat_failures[user_id] = 0
    
    except Exception as e:
        logging.error(f"❌ Failed to process registration for user {user_id}: {e}")
        if user_id in user_db:
           update_user_db(user_id=user_id, status="not-registered")
        return

def traffic_pause(user_id, ho_delay_ms: float) -> None:
    # Apply handover delay pause if configured (e.g., to allow user to switch satellite link or send back handover complete) as rate reduction to delay the packet scheduling on the new route
        if ho_delay_ms > 0:
            mtu = 1508  # Assuming MTU for shaping rules
            logging.info("⧴ Applying handover delay of %dms", ho_delay_ms)
            
            rate_kbit = max(1, int(mtu * 8 / ho_delay_ms))  # kbit/s (since ms in denominator)
            burst_bytes = mtu
            cburst_bytes = mtu
            minor = get_user_qdisc_minor(user_id)

            run_cmd([
            "tc","class","change","dev","veth0_rt",
            "parent","1:","classid",f"1:{minor}",
            "htb",
            "rate",f"{rate_kbit}kbit","ceil",f"{rate_kbit}kbit",
            "burst",f"{burst_bytes}b","cburst",f"{cburst_bytes}b",
            ])

            deadline = time.monotonic() + (ho_delay_ms / 1000.0)
            target_backlog_bytes = mtu
            while time.monotonic() < deadline:
                try:
                    class_stats = run_cmd_capture([
                        "tc", "-s", "class", "show",
                        "parent", "1:",
                        "classid", f"1:{minor}",
                        "dev", "veth0_rt",
                    ])
                    backlog_match = re.search(r"backlog\s+(\d+)b", class_stats)
                    # logging.info(f"⧴ Handover delay in progress, current backlog: {backlog_match.group(1) if backlog_match else 'N/A'} bytes")
                    if backlog_match and int(backlog_match.group(1)) >= target_backlog_bytes:
                        break
                except Exception:
                    pass
                time.sleep(0.001)
            
            # Restore original qdisc after delay
            run_cmd([
            "tc","class","change","dev","veth0_rt",
            "parent","1:","classid",f"1:{minor}",
            "htb",
            "rate","10gbit","ceil","10gbit",
            "burst","15kb","cburst","15kb",   # example “normal” values
            ])

            logging.info("⧴ Handover delay completed, restored original qdisc settings")

    
def handle_user_measurement_report(payload: Dict[str, Any]) -> None:
    try:
        user_id = payload["user_id"]
        user_sat_ipv6 = payload.get("user_sat_ipv6", payload.get("current_sat_ipv6", ""))
        user_dev = payload.get("user_dev", payload.get("current_sat_dev", ""))
        user_links_db = payload.get("user_links_db", payload.get("user_links_db", ""))
        if isinstance(user_links_db, str):
            user_links_db = json.loads(user_links_db) if user_links_db else {}
        update_user_db(user_id=user_id, user_links_db=user_links_db, user_dev=user_dev)
        if user_sat_ipv6:
            logging.debug("Updated user_sat_ipv6 from report for user %s: %s", user_id, user_sat_ipv6)
    except KeyError as e:
        logging.error(f"❌ Invalid link report payload: {e}")
    except Exception as e:
        logging.error(f"❌ Failed to process link report for user {user_id}: {e}")

def handle_user_hello(payload: Dict[str, Any]) -> None:
    user_id = payload.get("user_id", "")
    if user_id:
        with heartbeat_lock:
            heartbeat_failures[user_id] = 0

def handle_user_handover_complete(payload: Dict[str, Any]) -> None:
    logging.info(f"👤 Received handover complete from user {payload.get('user_id', '')}")
    upstream_sids = payload.get("upstream_sids", "")
    upstream_sid_list = [sid for sid in upstream_sids.split(",") if sid]
    downstream_sid_list = list(reversed(upstream_sid_list[:-1])) if len(upstream_sid_list) > 1 else []
    new_downstream_sids = ",".join(downstream_sid_list)

    new_grd_ipv6 = downstream_sid_list[0] if downstream_sid_list else ""
    if not new_grd_ipv6:
        logging.error(f"❌ No GRD IPv6 found in downstream SIDs from handover complete of user {payload.get('user_id', '')}, cannot complete handover")
        return
    new_grd_dev, _ = derive_egress_dev(new_grd_ipv6)
    if not new_grd_dev:
        logging.error(f"❌ Failed to derive GRD dev from IPv6 {new_grd_ipv6} in handover complete of user {payload.get('user_id', '')}, cannot complete handover")
        return
    new_user_dev = payload.get("user_dev", "")
    user_id = payload.get("user_id", "")
    user_ipv6 = payload.get("user_ipv6", "")

    ip_cmd = build_srv6_route_replace(dst_prefix=user_ipv6, sids=new_downstream_sids, dev=new_grd_dev, metric=20)
    run_cmd(ip_cmd)
    update_user_db(user_id=user_id, 
                    user_ipv6=user_ipv6, 
                    downstream_sids=new_downstream_sids, 
                    upstream_sids=payload.get("upstream_sids", ""),
                    grd_dev=new_grd_dev,
                    user_dev = new_user_dev,
                    status="registered")
    grd_sat_name = links_db.get(new_grd_dev, {}).get("remote_endpoint_name", "unknown")
    user_sat_name = user_db.get(user_id, {}).get("user_links_db", {}).get(new_user_dev, {}).get("remote_endpoint_name", "unknown")
    logging.info(f"✅  Handover completed for {user_id}. New grd satellite {grd_sat_name}, new user satellite {user_sat_name}, new downstream sids {new_downstream_sids}, new upstream sids {payload.get('upstream_sids', '')}")

def handle_user_request(
    sock: socket.socket,
    payload: Dict[str, Any],
    peer: Tuple[str, int, int, int],
    ho_delay_ms: float
) -> None:
    msg_type = str(payload.get("type", "")).strip().lower()
    if msg_type == "measurement_report":
        threading.Thread(
            target=handle_user_measurement_report,
            args=(dict(payload),),
            daemon=True,
            name=f"lr-handler-{payload.get('user_id', 'unknown')}",
        ).start()
    elif msg_type == "hello":
        threading.Thread(
            target=handle_user_hello,
            args=(dict(payload),),
            daemon=True,
            name=f"hello-handler-{payload.get('user_id', 'unknown')}",
        ).start()
    elif msg_type == "registration_request":
        threading.Thread(
            target=handle_user_registration_request,
            args=(sock, dict(payload), peer, ho_delay_ms),
            daemon=True,
            name=f"reg-handler-{payload.get('user_id', 'unknown')}",
        ).start()
    elif msg_type == "handover_complete":
        threading.Thread(
            target=handle_user_handover_complete,
            args=(dict(payload),),
            daemon=True,
            name=f"ho-complete-handler-{payload.get('user_id', 'unknown')}",
        ).start()
    else:
        logging.warning("❌ Unknown command type: %s", payload.get("type", "N/A"))

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
#   SERVER
# ----------------------------
def remove_qdisc_for_user(user_ipv6: str, user_id: str) -> None:
    dev = "veth0_rt"
    dst = user_ipv6.split("/")[0] if user_ipv6 else ""
    minor = get_user_qdisc_minor(user_id)
    if dst:
        try:
            run_cmd(["tc", "filter", "del", "dev", dev, "parent", "1:", "protocol", "ipv6", "prio", "10", "flower", "dst_ip", dst])
        except Exception:
            pass
    try:
        run_cmd(["tc", "class", "del", "dev", dev, "parent", "1:", "classid", f"1:{minor}"])
    except Exception:
        pass


def prepare_qdisc_for_new_user(user_ipv6: str, user_id: str) -> None:
    dev = "veth0_rt" # Assuming this is the shaping interface
    dst = user_ipv6.split("/")[0]  # Extract IP from possible prefix
    minor = get_user_qdisc_minor(user_id)
    run_cmd(["tc", "class", "add", "dev", dev, "parent", "1:", "classid", f"1:{minor}", "htb", "rate", "10gbit", "ceil", "10gbit"])
    run_cmd(["tc", "filter", "add", "dev", dev, "parent", "1:", "protocol", "ipv6", "prio", "10", "flower","dst_ip" ,dst, "action","pass","flowid" ,f"1:{minor}"])
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

def serve(bind_addr: str, port: int, ho_delay: float) -> None:
    # prepare qdisk for users (if ho_delay is set)
    init_qdisc()
    sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    sock.bind((bind_addr, port))
    logging.info("⚙️ Ground connection agent listening on [%s]:%d", bind_addr, port)

    while True:
        data, peer = sock.recvfrom(MAX_UDP_RECV_BYTES)
        try:            
            msg = json.loads(data.decode())
            handle_user_request(sock=sock, payload=msg, peer=peer, ho_delay_ms=ho_delay)
        except Exception as e:
            logging.warning("❌ Request failed from [%s]:%d: %s", peer[0], peer[1], e)


# ----------------------------
#   ENTRYPOINT
# ----------------------------
def main() -> None:
    global is_user_handover_needed, is_grd_handover_needed, process_connection_handover, grd_ipv6, user_callback_port, handover_metadata, link_duration_initial_value_s, link_setup_delay_s, max_links, handover_delay_ms, report_file
    ap = argparse.ArgumentParser()
    ap.add_argument("--bind", default="::", help="Address to bind the UDP server for handover (default: :: for all interfaces)")
    ap.add_argument("--port", type=int, default=5005, help="UDP port where grd listens for handover_request (default: 5005)")
    ap.add_argument("--local-address", help="IPv6 address of local node (Default: address found in /etc/hosts for the hostname)")
    ap.add_argument("--handover-strategy", type=str, default="lifetime", help="Handover strategy to use for handover decision (default: lifetime)")
    ap.add_argument("--handover-strategy-metadata", type=json.loads, default='{}', help="JSON string with metadata parameters for the handover strategy (e.g., threshold values, weights, etc.)")
    ap.add_argument("--handover-delay", type=float, help="Handover delay in mseconds (requires veth0_rt interface, use app/shaping-ns-create-v6.sh)", default=0)
    ap.add_argument("--usr-port", type=int, default=5006, help="Default UDP port where user agent listens for commands (default: 5006)")
    ap.add_argument("--sat-ipv6-prefix", default="2001:db8:100::/64", help="IPv6 prefix used for satellite IPv6 addresses (default: 2001:db8:100::/64)")
    ap.add_argument("--link-setup-delay", type=float, default=5, help="Estimated time in seconds needed by to setup relevat routes and interfaces after link creatio, default 5s)")
    ap.add_argument("--link-duration-initial-value", type=float, default=4*60, help="Initial value in seconds for the duration of new links, default: 4min)")
    ap.add_argument("--max-links", type=int, default=16, help="Max number of simultaneous links")
    ap.add_argument("--report", action="store_true", help="Enable detailed reporting of internal state for debugging")
    ap.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"), help="Logging level (default: INFO or value of LOG_LEVEL env var)")
    args = ap.parse_args()
    
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(message)s")
    refresh_hosts_ipv6_cache()
    
    if args.local_address is None:
        # Derive local IPv6 address from the loopback interface
        grd_ipv6 = resolve_ipv6_from_hosts(os.environ["NODE_NAME"])
        logging.debug("Derived local IPv6 address from /etc/hosts: %s", grd_ipv6)
    else:
        grd_ipv6 = args.local_address
        logging.debug("Using provided local IPv6 address: %s", grd_ipv6)

    user_callback_port = args.usr_port
    
    # Set handover strategy function based on argument
    if args.handover_strategy == "lifetime":
        is_user_handover_needed = lifetime_strategy_user_handover
        is_grd_handover_needed = lifetime_strategy_grd_handover
        process_connection_handover = lifetime_strategy_connection_handover
    else:        
        logging.error(f"Unsupported handover strategy: {args.handover_strategy}")
        sys.exit(1)
    handover_metadata = args.handover_strategy_metadata
    handover_delay_ms = args.handover_delay
    
    if args.report:
        report_file_name = f"report_{os.environ['NODE_NAME']}_conn_manager_grd.log"
        report_file = open(report_file_name, "w")
        logging.info(f"📊 Detailed reporting enabled, writing to {report_file_name}")

    if subprocess.run(
        ["ip", "link", "show", "veth0_rt"],
        text=True,
        capture_output=True,
    ).returncode != 0:
        logging.info("veth0_rt interface not found, creating shaping namespace for handover delay")
        run_cmd(["/app/shaping-ns-create-v6.sh"])
        
    
    # Start watching link actions in a separate thread
    etcd_client = get_etcd_client()
    link_setup_delay_s = args.link_setup_delay
    link_duration_initial_value_s = args.link_duration_initial_value
    max_links = args.max_links
    # Add grd to user_db to use handover stratey for the default route towards satellites. The route is stored in downstream_sids 
    
    user_db[os.environ["NODE_NAME"]] = {
        "user_ipv6": args.sat_ipv6_prefix,  # example IPv6 for the grd default route towards satellites (can be adjusted as needed)
        "upstream_sids": "",
        "downstream_sids": "",
        "grd_dev": "",
        "status": "registered",
        "qdisc_minor": None,
    }
    
    preload_links_db_from_etcd(etcd_client)
    
    # configure default route for satellites
    process_connection_handover(handover_metadata)
    new_grd_dev, needed = is_grd_handover_needed(os.environ["NODE_NAME"], handover_metadata)  # trigger initial handover decision for default route towards satellites based on initial links_db state (if any)
    if needed:
        new_sat_name = links_db.get(new_grd_dev, {}).get("remote_endpoint_name", "unknown")
        logging.info(f"🔀 Initial decision for {node_name} default route {args.sat_ipv6_prefix} via satellite {new_sat_name}")
        process_user_handover(os.environ["NODE_NAME"], new_grd_dev)
    
    # Start background thread to watch for link actions and update links_db accordingly
    threading.Thread(
        target=watch_link_actions_loop,
        args=(etcd_client,),
        daemon=True,
        name="watch-link-actions",
        ).start()
    
    # Start background thread to periodically evaluate local handover decisions for users based on the selected strategy and current links_db state
    threading.Thread(
        target=processing_handover_loop,
        args=(),
        daemon=True,
        name="local-handover-loop",
    ).start()
    threading.Thread(
        target=heartbeat_monitor_loop,
        args=(),
        daemon=True,
        name="heartbeat-monitor-loop",
    ).start()
    
    # Start UDP server to handle user registration and handover requests
    serve(bind_addr=args.bind, port=args.port, ho_delay=args.handover_delay)


if __name__ == "__main__":
    main()
