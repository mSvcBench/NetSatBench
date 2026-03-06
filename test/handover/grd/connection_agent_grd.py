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
link_duration_initial_value_s = 4*60  # initial value for link duration (sec)
is_local_handover_needed = None  # assign the handover strategy function to use for processing handover decisions based on links_db state (can be extended to more complex strategies as needed)
handover_metadata = {}  # metadata dict to pass to the handover strategy function (can include threshold values, weights, or other parameters needed for the strategy logic)
handover_periodic_check_s = 3.3  # periodic check interval for handover decision (can be tuned based on expected link dynamics and handover time requirements)
hosts_ipv6_cache: Dict[str, str] = {}
grd_ipv6 = ""
user_callback_port = 5006
_UNSET = object() # sentinel value to distinguish between "no update" and "update with None/empty" in db update functions

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


def update_user_db(user_id: str, user_ipv6: Any = _UNSET, upstream_sids: Any = _UNSET, downstream_sids: Any = _UNSET, dev: Any = _UNSET, status: Any = _UNSET) -> None:
    global user_db
    user_db[user_id] = {
        "user_ipv6": user_ipv6 if user_ipv6 is not _UNSET else user_db.get(user_id, {}).get("user_ipv6", None),
        "upstream_sids": upstream_sids if upstream_sids is not _UNSET else user_db.get(user_id, {}).get("upstream_sids", None),
        "downstream_sids": downstream_sids if downstream_sids is not _UNSET else user_db.get(user_id, {}).get("downstream_sids", None),
        "dev": dev if dev is not _UNSET else user_db.get(user_id, {}).get("dev", None),
        "status": status if status is not _UNSET else user_db.get(user_id, {}).get("status", None),
    }
    if user_id == node_name:
        user_db[user_id]["status"] = "registered"  # self user is always considered registered once we have its info in the db

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
                logging.warning("⚠️ Skipping malformed initial link entry: %s", e)
        logging.info("📥 Initial links preload completed: loaded=%d skipped=%d", loaded, skipped)
    except Exception as e:
        logging.warning("⚠️ Failed to preload initial links from Etcd: %s", e)

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
    ## Process PutEvent (Add/Update)
    if not isinstance(event, etcd3.events.PutEvent):
        logging.warning("⚠️ Ignoring non-PutEvent for handover processing.")
        return
    try:    
        link_dev = event.key.decode().split("/")[-1]  # Assuming key format includes dev_id at the end
        l = json.loads(event.value.decode())

        # Check if this is an update of available links
        if link_dev in links_db and links_db[link_dev].get("status") == "available":
            ep1, ep2 = l.get("endpoint1"), l.get("endpoint2")
            logging.info(f"🔄 Link update detected for {link_dev}: {ep1}<->{ep2}")
            update_link_db(link_dev=link_dev, etcd_link_data=l, last_updated=time.time())
        else:
            ep1, ep2 = l.get("endpoint1"), l.get("endpoint2")
            remote_endpoint = ep2 if ep1 == node_name else ep1
            remote_endpoint_ipv6 = resolve_ipv6_from_hosts(remote_endpoint) if remote_endpoint else None
            logging.info(f"➕ New link detected for {link_dev}: {ep1}<->{ep2}") 
            update_link_db(link_dev=link_dev, etcd_link_data=l, last_created=time.time(), last_updated=time.time(), status="available", last_duration=link_duration_initial_value_s, remote_endpoint_ipv6=remote_endpoint_ipv6)
        return
    except Exception as ex:
        logging.error("❌ Failed to process link action event %s", ex)

def handle_link_delete_action(event):
    ## Process DeleteEvent (Remove)
    if not isinstance(event, etcd3.events.DeleteEvent):
        logging.warning("⚠️ Ignoring non-DeleteEvent for handover processing.")
        return
    try:
        link_dev = event.key.decode().split("/")[-1]  # Assuming key format includes dev_id at the end
        if link_dev in links_db:
            logging.info(f"➖ Link deleted for {link_dev}: {links_db[link_dev].get('endpoint1')}<->{links_db[link_dev].get('endpoint2')}")
            update_link_db(link_dev=link_dev, status="unavailable", last_duration=time.time() - links_db[link_dev].get("last_created", time.time()))
        else:
            logging.warning(f"⚠️ Received delete event for unknown link device {link_dev}, ignoring.")
            return
    except Exception as ex:
        logging.error("❌ Failed to process link delete event %s", ex)
        return
    # Evaluate handover decision if any user was using the link that just got deleted
    for user_id, user_info in user_db.items():
        if user_info.get("dev") == link_dev:
            logging.info(f"⚠️ User {user_id} was using deleted link {link_dev}, evaluating handover decision...")
            update_user_db(user_id=user_id, dev=None)  # Update user_db with new status for the user during local handover processing
            dev, found = is_local_handover_needed(user_id, handover_metadata)
            if found:
                logging.info(f"🔀 Handover needed for user {user_id} due to link deletion, selected new link on dev {dev}")
                handle_local_handover(user_id, dev)
            else:
                logging.error(f"❌ No available link found for {user_id} after deletion of link {link_dev}, node is now without a connection")

def lifetime_strategy(user_id: str, metadata: dict) -> Tuple[str, bool]:
    # Example handover strategy: always prefer the link with greatest ttl
    threshold_s = metadata.get("threshold_s", link_duration_initial_value_s/4.0)  # threshold for minimum remaining duration to consider a handover
    # compute remaining duration for available links and select the one with the longest remaining duration above threshold
    grd_link = user_db.get(user_id, {}).get("dev", None)
    if grd_link == None:
        # no link currently assigned to user, so handover is needed to assign the best available link
        remaining_duration = 0
    elif links_db.get(grd_link,{}).get("status",None) != "available":
            # current link is not available, so handover is needed to assign the best available link
        remaining_duration = 0
    else:
        remaining_duration = links_db.get(grd_link, {}).get("last_duration", 0) - (time.time() - links_db.get(grd_link, {}).get("last_created", 0))
    
    if remaining_duration > threshold_s:
        # current link has enough remaining duration, no handover needed
        return grd_link, False
    
    available_devs = [(dev,l) for dev,l in links_db.items() if l.get("status") == "available"]
    if not available_devs:
        return "", False

    candidate_dev = max(available_devs, key=lambda x: x[1].get("last_duration", 0) - (time.time() - x[1].get("last_created", 0)))
    if candidate_dev[0] != user_db.get(user_id, {}).get("dev"):
        return candidate_dev[0],True
    else:
        return candidate_dev[0],False

def processing_local_handover_loop() -> None:
    while True:
        for user_id in user_db.keys():
            if user_db[user_id].get("status") != "registered":
                logging.warning(f"⚠️ Skipping handover processing for user {user_id} which is not in registered state")
                continue
            new_dev, local_handover_needed = is_local_handover_needed(user_id, handover_metadata)
            if local_handover_needed:
                logging.info(f"🔀 Handover decision for user {user_id}: selected newest link {new_dev}")
                handle_local_handover(user_id, new_dev)
        time.sleep(handover_periodic_check_s)  # periodic check interval for handover decision 


# def schedule_local_handover_processing() -> None:
#     global handover_processing_running, handover_processing_pending
#     with handover_processing_lock:
#         handover_processing_pending = True
#         if handover_processing_running:
#             return
#         handover_processing_running = True

#     def _worker() -> None:
#         global handover_processing_running, handover_processing_pending
#         try:
#             while True:
#                 with handover_processing_lock:
#                     if not handover_processing_pending:
#                         handover_processing_running = False
#                         return
#                     handover_processing_pending = False
#                 processing_local_handover()
#         except Exception as e:
#             logging.error("❌ Local handover async worker failed: %s", e)
#             with handover_processing_lock:
#                 handover_processing_running = False

#     threading.Thread(target=_worker, daemon=True, name="local-handover-processor").start()

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

def handle_local_handover(user_id,new_dev):
    # reroute user traffic to new link 
    user = user_db.get(user_id)
    if user.get("status") != "registered":
        logging.warning(f"⚠️ Attempted local handover for user {user_id} which is not in registered state, skipping")
        return
    update_user_db(user_id=user_id, status="handover_in_progress")  # Update user_db with new status for the user during local handover processing
    user_ipv6 = user.get("user_ipv6", "")
    old_grd_dev = user.get("dev", "")
    old_upstream_sids = user_db.get(user_id, {}).get("upstream_sids", "") # from user to grd
    old_downstream_sids = user_db.get(user_id, {}).get("downstream_sids", "") # from grd to user, old stored for rollback
    user_sat_ipv6 = old_upstream_sids.split(",")[0] if old_upstream_sids else ""  # Assuming the first SID in upstream SIDs is the user access satellite, 
    grd_sat_ipv6 = links_db.get(new_dev, {}).get("remote_endpoint_ipv6", "")
    
    # compute sids
    new_downstream_sids, new_upstream_sids = create_sids(grd_sat_ipv6, user_sat_ipv6)
    try:
        if not wait_for_link_local_via_route(grd_sat_ipv6, timeout_s=link_setup_delay_s):
            logging.warning(
                f"⚠️ No route with link-local next-hop for {grd_sat_ipv6} before local handover"
            )
        # inject new route
        ip_cmd = build_srv6_route_replace(dst_prefix=user_ipv6, sids=new_downstream_sids, dev=new_dev, metric=20)
        run_cmd(ip_cmd)

        # update user db 
        if user_id == node_name:
            update_user_db(user_id=user_id, user_ipv6=user_ipv6, downstream_sids=new_downstream_sids, dev=new_dev, status="registered")
            logging.info(f"✅ Local handover completed for user {user_id} to new link on dev {new_dev}")
            return  # skip sending handover command to self in case of local handover for the satellite subnet
        
        # send handover command to user to update upstream SIDs with new dev ipv6
        callback_port = user_callback_port
        txid = str(int(time.time() * 1000))
        cmd_msg = {
            "type": "handover_command_unsolicited",
            "txid": txid,
            "grd_id": os.environ["NODE_NAME"],
            "grd_ipv6": grd_ipv6,
            "sids": new_upstream_sids,
        }
        with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as notify_sock:
                send_udp_json(notify_sock, cmd_msg, (user_ipv6, callback_port, 0, 0))
        logging.info(f"✉️ Sent unsolicited handover command to user {user_id} with sid={new_upstream_sids}")
        logging.info(f"✅ Local handover completed for user {user_id} to new link on dev {new_dev}")
        update_user_db(user_id=user_id, user_ipv6=user_ipv6, upstream_sids=new_upstream_sids, downstream_sids=new_downstream_sids, dev=new_dev, status="registered")
    except Exception as e:
        try:       
            logging.error(f"❌ Local handover failed for user {user_id} to new link on dev {new_dev}: {e}")
            restore_cmd = build_srv6_route_replace(dst_prefix=user_ipv6, sids=old_downstream_sids, dev=old_grd_dev, metric=20)
            logging.info(f"🔄 Attempting to restore old route for user {user_id} on dev {old_grd_dev}")
            run_cmd(restore_cmd)
            update_user_db(user_id=user_id, upstream_sids=old_upstream_sids, downstream_sids=old_downstream_sids, dev=old_grd_dev, status="registered")
            logging.info(f"🔄 Restored old route for user {user_id}")
        except Exception as e:
            update_user_db(user_id=user_id, user_ipv6=user_ipv6, upstream_sids= None, downstream_sids=None, dev=None, status="not-registered")
            logging.error(f"❌ Failed to restore old route for user {user_id}: {e}")
    return


# ----------------------------
#   MAIN LOGIC FOR LINK MANAGEMENT USER SIDE
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
        user_sat_ipv6 = payload["init_sat_ipv6"]
    except KeyError as e:
        logging.error(f"❌ Invalid registration request payload: {e}")
        return

    logging.info(f"👤 Received registration request from {user_id}")
    user = user_db.get(user_id, None)
    if user is None:
        logging.info(f"👤 New user {user_id} registering for the first time")
        update_user_db(user_id=user_id, user_ipv6=user_ipv6, status="not-registered")  # initialize user_db entry for the new user
        if ho_delay_ms > 0:
            prepare_qdisc_for_new_user(user_ipv6=user_ipv6, user_id=user_id)
        user = user_db.get(user_id)
    try:
        if user.get("status") == "registration_in_progress":
            logging.warning(f"⚠️ Registration already in progress for user {user_id}, ignoring duplicate registration request")
            return
        update_user_db(user_id=user_id, user_ipv6=user_ipv6, status="registration_in_progress")
        grd_dev, found = is_local_handover_needed(user_id, handover_metadata) # chose of the grd dev to serve the user
        if grd_dev == "":
            logging.warning(f"⚠️ No suitable access satellite found for user {user_id}, registration aborted")
            # reset user state
            update_user_db(user_id=user_id, user_ipv6=user_ipv6, upstream_sids= None, downstream_sids=None, dev=None, status="not-registered")
            return
    
        grd_sat_ipv6 = links_db.get(grd_dev, {}).get("remote_endpoint_ipv6", "") if grd_dev else ""

        # build sids
        downstream_sids, upstream_sids = create_sids(grd_sat_ipv6, user_sat_ipv6)

        # route injection
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
        logging.info(f"✅ Registration completed for user {user_id} with upstream sid {upstream_sids} and downstream sid {downstream_sids} on grd dev {grd_dev}")

        update_user_db(
            user_id=user_id,
            user_ipv6=user_ipv6,
            upstream_sids=upstream_sids,
            downstream_sids=downstream_sids,
            dev=grd_dev,
            status="registered"
        )
    
    except Exception as e:
        logging.error(f"❌ Failed to process registration for user {user_id}: {e}")
        if user_id in user_db:
           update_user_db(user_id=user_id, status="not-registered")
        return

def handle_user_handover_request(
    sock: socket.socket,
    payload: Dict[str, Any],
    peer: Tuple[str, int, int, int],
    ho_delay_ms: float
) -> None:

    try:
        user_id = payload["user_id"]
        user_ipv6 = payload["user_ipv6"]
        new_user_sat_ipv6 = payload["new_sat_ipv6"]
    except KeyError as e:
        logging.error(f"❌ Invalid handover request payload: {e}")
        return

    logging.info(f"🔀 Received handover request from {user_id} for new satellite {new_user_sat_ipv6}")
    user = user_db.get(user_id, None)
    if user is None or user.get("status") != "registered":
        logging.warning(f"⚠️ Received handover request from unregistered user {user_id}, ignoring")
        return
    
    try:
        update_user_db(user_id=user_id, status="handover_in_progress")  # Update user_db with new status for the user during handover processing
        # compute downstream sids
        old_downstream_sids = user_db.get(user_id, {}).get("downstream_sids", "") # from grd to user, old stored for rollback
        old_upstream_sids = user_db.get(user_id, {}).get("upstream_sids", "") # from user to grd, old stored for rollback
        grd_dev = user_db.get(user_id, {}).get("dev", "")
        grd_sat_ipv6 = links_db.get(grd_dev, {}).get("remote_endpoint_ipv6", "") if grd_dev else ""
        
        # build sids
        new_downstream_sids, new_upstream_sids = create_sids(grd_sat_ipv6, new_user_sat_ipv6)
        
        # Sending handover command to usr
        callback_port = payload.get("callback_port", user_callback_port)  # Optional port to send handover_command back to usr
        txid = payload.get("txid", str(int(time.time() * 1000))) # nonce txid for correlation (default: current timestamp in ms)
        cmd_msg = {
            "type": "handover_command",
            "txid": txid,
            "grd_id": os.environ["NODE_NAME"],  
            "grd_ipv6": grd_ipv6,  # IPv6 usr should use to reach grd (e.g., "2001:db8:100::2/128")
            "sids": new_upstream_sids,  # SID usr must use to reach grd
        }
        
        peer_for_cmd = (peer[0], callback_port, peer[2], peer[3])  # keep flowinfo/scopeid
        send_udp_json(sock, cmd_msg, peer_for_cmd)
        logging.info(f"✉️ Sent handover command to user {user_id}")
        
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

        # inject route with new downstream sid on grd
        ip_cmd = build_srv6_route_replace(dst_prefix=user_ipv6, sids=new_downstream_sids, dev=grd_dev)
        run_cmd(ip_cmd)
        
        # update user db
        update_user_db(user_id=user_id, upstream_sids=new_upstream_sids, downstream_sids=new_downstream_sids, dev=grd_dev, status="registered")
        logging.info(f"✅ Handover completed for user {user_id} with upstream sid {new_upstream_sids} and downstream sid {new_downstream_sids} on grd dev {grd_dev}")
    except Exception as e:
        logging.error(f"❌ Failed to process handover for user {user_id}: {e}")
        try:
            # attempt to restore old route on grd
            restore_cmd = build_srv6_route_replace(dst_prefix=user_ipv6, sids=old_downstream_sids, dev=grd_dev)
            logging.info(f"🔄 Attempting to restore old route for user {user_id}")
            run_cmd(restore_cmd)
            update_user_db(user_id=user_id, upstream_sids=old_upstream_sids, downstream_sids=old_downstream_sids, dev=grd_dev, status="registered")
            logging.info(f"🔄 Restored old route for user {user_id} with upstream sid {old_upstream_sids} and downstream sid {old_downstream_sids} on grd dev {grd_dev} ")
        except Exception as e:
            logging.error(f"❌ Failed to restore old route for user {user_id}: {e}, user moved in not-registered state")
            update_user_db(user_id=user_id, user_ipv6=user_ipv6, upstream_sids= None, downstream_sids=None, dev=None, status="not-registered")
        return
    
def handle_user_request(
    sock: socket.socket,
    payload: Dict[str, Any],
    peer: Tuple[str, int, int, int],
    ho_delay_ms: float
) -> None:
    # Validate
    if payload.get("type") == "handover_request":
        threading.Thread(
            target=handle_user_handover_request,
            args=(sock, dict(payload), peer, ho_delay_ms),
            daemon=True,
            name=f"ho-handler-{payload.get('user_id', 'unknown')}",
        ).start()
    elif payload.get("type") == "registration_request":
        threading.Thread(
            target=handle_user_registration_request,
            args=(sock, dict(payload), peer, ho_delay_ms),
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
    
def serve(bind_addr: str, port: int, ho_delay: float) -> None:
    # prepare qdisk for users (if ho_delay is set)
    if ho_delay > 0:
        init_qdisc()
    sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    sock.bind((bind_addr, port))
    logging.info("⚙️ Ground connection agent listening on [%s]:%d", bind_addr, port)

    while True:
        data, peer = sock.recvfrom(4096)
        try:            
            msg = json.loads(data.decode())
            handle_user_request(sock=sock, payload=msg, peer=peer, ho_delay_ms=ho_delay)
        except Exception as e:
            logging.warning("❌ Request failed from [%s]:%d: %s", peer[0], peer[1], e)


def main() -> None:
    global is_local_handover_needed, link_setup_delay_s, grd_ipv6, user_callback_port, handover_metadata, link_duration_initial_value_s
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
        is_local_handover_needed = lifetime_strategy
    else:        
        logging.error(f"Unsupported handover strategy: {args.handover_strategy}")
        sys.exit(1)
    handover_metadata = args.handover_strategy_metadata
    # Start watching link actions in a separate thread
    etcd_client = get_etcd_client()
    link_setup_delay_s = args.link_setup_delay
    link_duration_initial_value_s = args.link_duration_initial_value
    # Add grd to user_db to use handover stratey for the default route towards satellites. The route is stored in downstream_sids 
    
    user_db[os.environ["NODE_NAME"]] = {
        "user_ipv6": args.sat_ipv6_prefix,  # example IPv6 for the grd default route towards satellites (can be adjusted as needed)
        "upstream_sids": "",
        "downstream_sids": "",
        "dev": "",
        "status": "registered",
    }
    
    preload_links_db_from_etcd(etcd_client)
    
    # configure default route for satellites
    new_dev, local_handover_needed = is_local_handover_needed(os.environ["NODE_NAME"], handover_metadata)  # trigger initial handover decision for default route towards satellites based on initial links_db state (if any)
    if local_handover_needed:
        logging.info(f"🔀 Initial decision for grd route {args.sat_ipv6_prefix} towards satellites via {new_dev}")
        handle_local_handover(os.environ["NODE_NAME"], new_dev)
    
    # Start background thread to watch for link actions and update links_db accordingly
    threading.Thread(
        target=watch_link_actions_loop,
        args=(etcd_client,),
        daemon=True,
        name="watch-link-actions",
        ).start()
    
    # Start background thread to periodically evaluate local handover decisions for users based on the selected strategy and current links_db state
    threading.Thread(
        target=processing_local_handover_loop,
        args=(),
        daemon=True,
        name="local-handover-loop",
    ).start()
    
    # Start UDP server to handle user registration and handover requests
    serve(bind_addr=args.bind, port=args.port, ho_delay=args.handover_delay)


if __name__ == "__main__":
    main()
