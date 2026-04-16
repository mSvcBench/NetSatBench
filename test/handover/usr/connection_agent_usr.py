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
import math


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
KEY_NODES_PREFIX = "/config/nodes/"
link_setup_delay_s = 0.2 # estimated time needed by sat-agent to setup relevat routes
registration_accept_timeout_s = None
reporting_period_s = 3.3 # periodic check interval for handover decision 
link_duration_initial_value_s = 4*60  # initial value for link duration (sec)
heartbeat_interval_s = 1.0
heartbeat_max_failures = 3
heartbeat_failures = 0
heartbeat_lock = threading.Lock()
MAX_UDP_RECV_BYTES = 65535
report_file = None  # file handle for detailed report output, if enabled by args
measurement_top_n_links = 5
select_measurement_report_links = None # function handle for selecting best link to send in the measurement report
satellite_nodes_db: Dict[str, Dict[str, Any]] = {}
COMPACT_LINK_REPORT_FMT = "cldb1"
STATUS_TO_CODE = {
    "unavailable": 0,
    "available": 1,
    "connected": 2,
}
CODE_TO_STATUS = {
    0: "unavailable",
    1: "available",
    2: "connected",
}

# Status not_registered, registration_in_progress, registered, handover_in_progress
status = "not_registered" # initial status before registration
current_dev = None # current iface used for data transfer

# ho eligibility strategy function, set in main() based on args
chose_reg_device = None
grd_ipv6 = None
grd_port = None
grd_id = None
user_callback_port = None
local_ipv6 = None
etcd_client = None
registration_timeout_timer = None
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
    user_callback_port: int,
    init_sat_dev: str, 
    user_links_db: Dict[str, Dict[str, Any]],
) -> None:
    msg: Dict[str, Any] = {
        "type": "registration_request",
        "user_id": os.environ["NODE_NAME"],
        "user_ipv6": user_ipv6,
        "init_sat_dev": init_sat_dev,
        "callback_port": user_callback_port,
        "user_links_db": user_links_db,
    }
    report_size_bytes = len(json.dumps(user_links_db).encode("utf-8"))
    payload_size_bytes = len(json.dumps(msg).encode("utf-8"))
    logging.debug(
        "📏 Registration link report size: %d bytes across %d link(s); UDP JSON payload: %d bytes",
        report_size_bytes,
        len(user_links_db),
        payload_size_bytes,
    )
    send_udp_json(grd_ipv6, grd_port, msg)


def send_link_report_udp(
    grd_ipv6: str,
    grd_port: int,
    user_dev: str,
    user_links_db: Dict[str, Any],
) -> None:
    msg: Dict[str, Any] = {
        "type": "measurement_report",
        "user_id": os.environ["NODE_NAME"],
        "user_dev": user_dev,
        "user_links_db": user_links_db,
    }
    report_size_bytes = len(json.dumps(user_links_db).encode("utf-8"))
    payload_size_bytes = len(json.dumps(msg).encode("utf-8"))
    reported_sats: List[str] = []
    report_links_count = 0
    if user_links_db.get("_fmt") == COMPACT_LINK_REPORT_FMT:
        for row in user_links_db.get("r", []):
            if not isinstance(row, list) or len(row) < 9:
                continue
            report_links_count += 1
            status_code = row[1]
            status = CODE_TO_STATUS.get(status_code, "unavailable")
            if status == "available":
                reported_sats.append(row[8] if row[8] else "unknown")
    else:
        for _link_dev, link_info in user_links_db.items():
            if not isinstance(link_info, dict):
                continue
            report_links_count += 1
            if link_info.get("status") == "available":
                reported_sats.append(link_info.get("remote_endpoint_name", "unknown"))
    logging.debug(
        "📏 Measurement link report size: %d bytes across %d link(s); UDP JSON payload: %d bytes; Reported satellites: %s",
        report_size_bytes,
        report_links_count,
        payload_size_bytes,
        ", ".join(reported_sats) if reported_sats else "-",
    )
    send_udp_json(grd_ipv6, grd_port, msg)

def send_hello_udp(
    grd_ipv6: str,
    grd_port: int,
) -> None:
    msg: Dict[str, Any] = {
        "type": "hello",
        "user_id": os.environ["NODE_NAME"],
        "grd_id": grd_id,
        "txid": str(int(time.time() * 1000)),
    }
    send_udp_json(grd_ipv6, grd_port, msg)

def send_handover_complete_udp(
    grd_ipv6: str,
    grd_port: int,
    user_dev: str,
    user_ipv6: str,
    upstream_sids: str,
) -> None:
    msg: Dict[str, Any] = {
        "type": "handover_complete",
        "user_id": os.environ["NODE_NAME"],
        "user_dev": user_dev,
        "user_ipv6": user_ipv6,
        "upstream_sids": upstream_sids,
        "grd_id": grd_id,
        "txid": str(int(time.time() * 1000)),
    }
    send_udp_json(grd_ipv6, grd_port, msg)

def derive_egress_dev(addr: str) -> str:
    out = run_cmd_capture(["ip", "-6", "route", "get", addr])
    dev_match = DEV_RE.search(out)
    if not dev_match:
        raise RuntimeError(f"❌ Could not parse egress dev from: {out}")
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
    logging.info(f"🎛️ Preparing qdisc for ground station {grd_id} with IPv6 {grd_ipv6}")
    dev = "veth0_rt" # Assuming this is the shaping interface
    dst = grd_ipv6.split("/")[0]  # Extract IP from prefix
    # derive user id as the position of username in the user_list 
    idx = grd_list.index(grd_id)
    classid = f"1:{idx+10}"

    # Rebuild the per-GRD shaping state in case a previous run left stale tc objects behind.
    try:
        run_cmd(["tc", "filter", "del", "dev", dev, "parent", "1:", "protocol", "ipv6", "prio", "10", "flower", "dst_ip", dst])
    except:
        pass
    try:        
        run_cmd(["tc", "class", "del", "dev", dev, "parent", "1:", "classid", classid])
    except:
        pass
    time.sleep(0.1)  # brief pause to ensure tc state is consistent before re-adding
    qdisc_class_cmd =  ["tc", "class", "add", "dev", dev, "parent", "1:", "classid", classid, "htb", "rate", "10gbit", "ceil", "10gbit"]
    logging.debug(f"🎛️ Applying tc command: {' '.join(qdisc_class_cmd)}")
    run_cmd(qdisc_class_cmd)
    qdisc_filter_cmd = ["tc", "filter", "add", "dev", dev, "parent", "1:", "protocol", "ipv6", "prio", "10", "flower", "dst_ip", dst, "action", "pass", "flowid", classid]
    logging.debug(f"🎛️ Applying tc command: {' '.join(qdisc_filter_cmd)}")
    run_cmd(qdisc_filter_cmd)
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

def update_links_db(link_dev: str, etcd_link_data: Any = _UNSET, last_created: Any = _UNSET, last_updated: Any = _UNSET, status: Any = _UNSET, last_duration: Any = _UNSET, remote_endpoint_ipv6: Any = _UNSET) -> None:
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

def on_registration_accept_timeout() -> None:
    global status, current_dev, etcd_client, registration_timeout_timer
    registration_timeout_timer = None
    if status != "registration_in_progress":
        return

    logging.warning("⚠️ Registration accept timeout reached. Resetting state and retrying registration.")
    status = "not_registered"
    current_dev = None

    if etcd_client is not None:
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

def build_link_registration_report(selected_dev: str | None = None) -> Dict[str, Any]:
    if selected_dev:
        link_info = links_db.get(selected_dev, {})
        if link_info.get("status") == "available":
            return {selected_dev: link_info}
        return {}

    sat_report = {}
    for link_dev, link_info in links_db.items():
        if link_info.get("status") == "available":
            sat_report[link_dev] = link_info
    return sat_report


def build_measurement_links_report() -> Dict[str, Any]:
    rows: List[List[Any]] = []
    for link_dev in select_measurement_report_links(current_dev, measurement_top_n_links):
        link_info = links_db.get(link_dev, {})
        if not link_info:
            continue
        rows.append([
            link_dev,
            STATUS_TO_CODE.get(link_info.get("status"), 0),
            link_info.get("last_duration"),
            link_info.get("last_created"),
            link_info.get("rate"),
            link_info.get("delay"),
            link_info.get("loss"),
            link_info.get("remote_endpoint_ipv6"),
            link_info.get("remote_endpoint_name"),
        ])
    return {
        "_fmt": COMPACT_LINK_REPORT_FMT,
        "r": rows,
    }


def remaining_link_duration_s(link_info: Dict[str, Any]) -> float:
    return link_info.get("last_duration", 0) - (time.time() - link_info.get("last_created", 0))

def link_delay_ms(link_info: Dict[str, Any]) -> float:
    delay_str = link_info.get("delay")
    return parse_delay(delay_str) if delay_str else 0


def select_measurement_report_links_grid_lifetime(selected_dev: str | None, top_n: int) -> List[str]:
    chosen_links: List[str] = []

    if selected_dev and selected_dev in links_db:
        chosen_links.append(selected_dev)

    user_sat = links_db.get(selected_dev, {}).get("remote_endpoint_name", "") if selected_dev else ""
    if not user_sat:
        return chosen_links

    try:
        user_sat_orbit, _user_sat_num_in_orbit = get_satellite_orbit_num(user_sat)
    except Exception as e:
        logging.warning("⚠️ Could not derive grid-plus neighbors for %s: %s", user_sat, e)
        return chosen_links

    user_shell_config = satellite_nodes_db.get(user_sat, {}).get("shell_config", {})
    total_orbits = user_shell_config.get("number_of_orbit", 0)
    if not total_orbits:
        return chosen_links

    left_orbit = user_sat_orbit - 1 if user_sat_orbit > 1 else total_orbits
    right_orbit = user_sat_orbit + 1 if user_sat_orbit < total_orbits else 1

    same_orbit_candidates: List[Tuple[str, Dict[str, Any]]] = []
    candidate_links_left_orbit: List[Tuple[str, Dict[str, Any]]] = []
    candidate_links_right_orbit: List[Tuple[str, Dict[str, Any]]] = []
    for link_dev, link_info in links_db.items():
        if link_dev == selected_dev or link_info.get("status") != "available":
            continue
        remote_sat = link_info.get("remote_endpoint_name", "")
        if not remote_sat:
            continue
        try:
            sat_orbit, _sat_num_in_orbit = get_satellite_orbit_num(remote_sat)
        except Exception:
            continue

        if sat_orbit == user_sat_orbit:
            same_orbit_candidates.append((link_dev, link_info))
        elif sat_orbit == left_orbit:
            candidate_links_left_orbit.append((link_dev, link_info))
        elif sat_orbit == right_orbit:
            candidate_links_right_orbit.append((link_dev, link_info))

    same_orbit_candidates.sort(
        key=lambda item: remaining_link_duration_s(item[1]),
        reverse=True,
    )
    candidate_links_left_orbit.sort(
        key=lambda item: remaining_link_duration_s(item[1]),
        reverse=True,
    )
    candidate_links_right_orbit.sort(
        key=lambda item: remaining_link_duration_s(item[1]),
        reverse=True,
    )

    extra_candidates: List[str] = []
    extra_candidates.extend([link_dev for link_dev, _ in same_orbit_candidates[:4]])
    extra_candidates.extend([link_dev for link_dev, _ in candidate_links_left_orbit[:4]])
    extra_candidates.extend([link_dev for link_dev, _ in candidate_links_right_orbit[:4]])

    max_total = max(top_n, 0)
    remaining_slots = max(max_total - len(chosen_links), 0)
    for link_dev in extra_candidates[:remaining_slots]:
        if link_dev not in chosen_links:
            chosen_links.append(link_dev)

    return chosen_links

def select_measurement_report_links_grid_delay(selected_dev: str | None, top_n: int) -> List[str]:
    chosen_links: List[str] = []

    if selected_dev and selected_dev in links_db:
        chosen_links.append(selected_dev)

    user_sat = links_db.get(selected_dev, {}).get("remote_endpoint_name", "") if selected_dev else ""
    if not user_sat:
        return chosen_links

    try:
        user_sat_orbit, _user_sat_num_in_orbit = get_satellite_orbit_num(user_sat)
    except Exception as e:
        logging.warning("⚠️ Could not derive grid-plus neighbors for %s: %s", user_sat, e)
        return chosen_links

    user_shell_config = satellite_nodes_db.get(user_sat, {}).get("shell_config", {})
    total_orbits = user_shell_config.get("number_of_orbit", 0)
    if not total_orbits:
        return chosen_links

    left_orbit = user_sat_orbit - 1 if user_sat_orbit > 1 else total_orbits
    right_orbit = user_sat_orbit + 1 if user_sat_orbit < total_orbits else 1

    same_orbit_candidates: List[Tuple[str, Dict[str, Any]]] = []
    candidate_links_left_orbit: List[Tuple[str, Dict[str, Any]]] = []
    candidate_links_right_orbit: List[Tuple[str, Dict[str, Any]]] = []
    for link_dev, link_info in links_db.items():
        if link_dev == selected_dev or link_info.get("status") != "available":
            continue
        remote_sat = link_info.get("remote_endpoint_name", "")
        if not remote_sat:
            continue
        try:
            sat_orbit, _sat_num_in_orbit = get_satellite_orbit_num(remote_sat)
        except Exception:
            continue

        if sat_orbit == user_sat_orbit:
            same_orbit_candidates.append((link_dev, link_info))
        elif sat_orbit == left_orbit:
            candidate_links_left_orbit.append((link_dev, link_info))
        elif sat_orbit == right_orbit:
            candidate_links_right_orbit.append((link_dev, link_info))

    same_orbit_candidates.sort(
        key=lambda item: link_delay_ms(item[1]),
        reverse=True,
    )
    candidate_links_left_orbit.sort(
        key=lambda item: link_delay_ms(item[1]),
        reverse=True,
    )
    candidate_links_right_orbit.sort(
        key=lambda item: link_delay_ms(item[1]),
        reverse=True,
    )

    extra_candidates: List[str] = []
    extra_candidates.extend([link_dev for link_dev, _ in same_orbit_candidates[:4]])
    extra_candidates.extend([link_dev for link_dev, _ in candidate_links_left_orbit[:4]])
    extra_candidates.extend([link_dev for link_dev, _ in candidate_links_right_orbit[:4]])

    max_total = max(top_n, 0)
    remaining_slots = max(max_total - len(chosen_links), 0)
    for link_dev in extra_candidates[:remaining_slots]:
        if link_dev not in chosen_links:
            chosen_links.append(link_dev)

    return chosen_links

def write_report_event(event_type: str, strategy_type: str, grd_name: str, sat_name: str) -> None:
    if report_file is None:
        return
    report_file.write(
        f"{time.time()},{event_type},{os.environ['NODE_NAME']},{strategy_type},{grd_name},{sat_name}\n"
    )
    report_file.flush()

def latilong_distance(lat1, long1, lat2, long2) -> float:
    # Haversine formula to calculate distance between two lat/long points on Earth surface
    R = 6371000.0  # Earth's radius in meters
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(long2 - long1)
    a = math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    surface_distance = R * c
    return surface_distance

def get_satellite_orbit_num(sat_name: str) -> Tuple[int,int]:
    satellite_metadata = satellite_nodes_db.get(sat_name, {})
    sat_config = satellite_metadata.get("sat_config", {})
    sat_orbit = sat_config.get("sat_orbit")
    sat_num_in_orbit = sat_config.get("sat_num_in_orbit")
    if sat_orbit is None or sat_num_in_orbit is None:
        raise RuntimeError(f"Satellite '{sat_name}' has no sat_orbit or sat_num_in_orbit metadata")
    return int(sat_orbit),int(sat_num_in_orbit)

# ----------------------------
#   MAIN LOGIC
# ----------------------------

def preload_satellite_nodes_db_from_etcd() -> None:
    loaded = 0
    skipped = 0
    logging.info("📥 Preloading satellite node metadata from Etcd prefix %s", KEY_NODES_PREFIX)
    try:
        for value, metadata in etcd_client.get_prefix(KEY_NODES_PREFIX):
            if not value:
                skipped += 1
                continue
            try:
                key = metadata.key.decode() if metadata and metadata.key else ""
                node_name_from_key = key.split("/")[-1] if key else ""
                node_payload = json.loads(value.decode())

                if (
                    isinstance(node_payload, dict)
                    and node_name_from_key in node_payload
                    and isinstance(node_payload[node_name_from_key], dict)
                ):
                    node_payload = node_payload[node_name_from_key]

                if not isinstance(node_payload, dict):
                    skipped += 1
                    continue
                if node_payload.get("type") != "satellite":
                    skipped += 1
                    continue

                satellite_name = node_name_from_key or node_payload.get("name")
                if not satellite_name:
                    skipped += 1
                    continue

                satellite_metadata = node_payload.get("metadata")
                if not isinstance(satellite_metadata, dict):
                    skipped += 1
                    continue

                satellite_nodes_db[satellite_name] = satellite_metadata
                loaded += 1
            except Exception as e:
                skipped += 1
                logging.error("❌ Skipping malformed node entry: %s", e)
        logging.info("📥 Satellite node preload completed: loaded=%d skipped=%d", loaded, skipped)
    except Exception as e:
        logging.error("❌ Failed to preload satellite node metadata from Etcd: %s", e)

def preload_links_db_from_etcd() -> None:
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
                update_links_db(link_dev=link_dev, etcd_link_data=l, last_created=time.time(), last_updated=time.time(), status="available", last_duration=link_duration_initial_value_s, remote_endpoint_ipv6=remote_endpoint_ipv6)
                if "expected_duration" in l:
                    expected_duration_s = parse_expected_duration(l["expected_duration"])
                    update_links_db(link_dev=link_dev, last_duration=expected_duration_s)
                    logging.debug(f"⏱️ Preloaded link {link_dev} with expected duration {expected_duration_s:.1f}s from Etcd")
                loaded += 1
            except Exception as e:
                skipped += 1
                logging.error("❌ Skipping malformed initial link entry: %s", e)
        logging.info("📥 Initial links preload completed: loaded=%d skipped=%d", loaded, skipped)
    except Exception as e:
        logging.error("❌ Failed to preload initial links from Etcd: %s", e)

def chose_proximity_grd():
    user_data = etcd_client.get("/config/nodes/" + node_name)
    if not user_data or not user_data[0]:
        logging.error(f"❌ No configuration found in Etcd for node {node_name} under /config/nodes/. Cannot derive proximity-based GRD.")
        return None
    try:
        user_config = json.loads(user_data[0].decode("utf-8"))
    except Exception as e:
        logging.error(f"❌ Failed to parse user configuration from Etcd for node {node_name}: {e}")
        return None
    
    local_position = user_config.get("metadata", {}).get("location", {})
    if not local_position:
        logging.error(f"❌ No location metadata found in user configuration for node {node_name}. Cannot derive proximity-based GRD.")
        return None
    best_grd = None
    lowest_distance = float("inf")
    
    for value, metadata in etcd_client.get_prefix("/config/nodes/"):
        if not value:
            continue
        try:
            gateway_config = json.loads(value.decode("utf-8"))
        except Exception:
            continue
        if gateway_config.get("type") != "gateway":
            continue
        gateway_name = metadata.key.decode("utf-8").rsplit("/", 1)[-1]
        gateway_position = gateway_config.get("metadata", {}).get("location", {})
        if not gateway_position:
            logging.warning(f"⚠️ No location metadata found in configuration for ground station {gateway_name}.")
        distance = latilong_distance(
            local_position.get("latitude", 0),
            local_position.get("longitude", 0),
            gateway_position.get("latitude", 0),
            gateway_position.get("longitude", 0),
        )
        if distance < lowest_distance:
            lowest_distance = distance
            best_grd = gateway_name
    return best_grd


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
    init_dev, _ = chose_reg_device(reg_metadata) # chose of the initial dev to serve the user based on handover strategy 
    init_sat_name = links_db.get(init_dev, {}).get("remote_endpoint_name", "") if init_dev else ""
    if init_dev != "":
        init_sat_ipv6 = links_db.get(init_dev, {}).get("remote_endpoint_ipv6", "")
        if not init_sat_ipv6:
            logging.info(f"❌ Failed to resolve access satellite IPv6 address for satellite {init_sat_name}")
            return
        logging.info(f"🛰️ Chosen access satellite {init_sat_name}. Registering...")
        try:
            status = "registration_in_progress"
            # add route to grd via initial satellite to ensure registration request can reach the grd
            if not wait_for_link_local_via_route(init_sat_ipv6, timeout_s=link_setup_delay_s):
                logging.warning(
                    f"⚠️ No route with link-local next-hop for {init_sat_ipv6} before handover request timeout window."
                )
            ip_cmd = build_srv6_route_replace(grd_ipv6, init_sat_ipv6, init_dev)
            run_cmd(ip_cmd)
            links_report = build_link_registration_report(init_dev)
            send_registration_request_udp(
                grd_ipv6=grd_ipv6,
                grd_port=grd_port,
                user_ipv6=local_ipv6,
                user_callback_port=user_callback_port,
                init_sat_dev=init_dev,
                user_links_db=links_report,
            )
            current_dev = init_dev
            start_registration_timeout()
            # For this example, we just log the registration action.
            logging.info(f"✉️ Sent registration request to {grd_id}.")
        except Exception as e:
            logging.error(f"❌ Failed to send registration request: {e}")
            status = "not_registered"
            current_dev = None
    else:
        logging.warning("⚠️ No suitable access satellite found for registration.")

def lifetime_strategy(metadata: dict) -> Tuple[str, bool]:
    # Example strategy: always prefer the link with greatest ttl
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

def reporting_loop() -> None:
    while True:
        if status != "registered":
            time.sleep(reporting_period_s)
            continue
        links_report = build_measurement_links_report()
        send_link_report_udp(
            grd_ipv6=grd_ipv6,
            grd_port=grd_port,
            user_dev=current_dev,
            user_links_db=links_report,
        )
        time.sleep(reporting_period_s)  # periodic check interval for handover decision (can be tuned based on expected link dynamics and handover time requirements)

def heartbeat_loop() -> None:
    global status, current_dev, heartbeat_failures
    while True:
        if status == "registered":
            try:
                send_hello_udp(grd_ipv6=grd_ipv6, grd_port=grd_port)
                with heartbeat_lock:
                    heartbeat_failures += 1
                    misses = heartbeat_failures
                if misses >= heartbeat_max_failures:
                    logging.warning("⚠️ Missed %d heartbeat ACKs from %s; resetting to not_registered and retrying registration.", misses, grd_id)
                    status = "not_registered"
                    current_dev = None
                    with heartbeat_lock:
                        heartbeat_failures = 0
                    cancel_registration_timeout()
                    handle_registration_request()
            except Exception as e:
                logging.error("❌ Failed to send heartbeat HELLO to %s: %s", grd_id, e)
        time.sleep(heartbeat_interval_s)


# ----------------------------
#   COMMAND HANDLERS
# ----------------------------

def traffic_pause(ho_delay_ms: float) -> None:
    # Apply handover delay rate reduction if configured (e.g., to allow user to switch satellite link)
    if ho_delay_ms > 0:
        logging.info("⧴ Applying handover delay of %dms", ho_delay_ms)
        mtu = 1508  # Assuming MTU for shaping rules
        rate_kbit = max(1, int(mtu * 8 / ho_delay_ms))  # kbit/s (since ms in denominator)
        burst_bytes = mtu
        cburst_bytes = mtu
        idx = grd_list.index(grd_id) if grd_id in grd_list else 0

        run_cmd([
        "tc","class","change","dev","veth0_rt",
        "parent","1:","classid",f"1:{idx+10}",
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
                    "classid", f"1:{idx+10}",
                    "dev", "veth0_rt",
                ])
                backlog_match = re.search(r"backlog\s+(\d+)b", class_stats)
                # logging.info(f"⧴ Handover delay in progress, current backlog: {backlog_match.group(1) if backlog_match else 'N/A'} bytes")
                if backlog_match and int(backlog_match.group(1)) >= target_backlog_bytes:
                    break
            except Exception:
                pass
            time.sleep(0.001)
        
        # Restore original qdisc after delay
        run_cmd([
        "tc","class","change","dev","veth0_rt",
        "parent","1:","classid",f"1:{idx+10}",
        "htb",
        "rate","10gbit","ceil","10gbit",
        "burst","15kb","cburst","15kb",   # example “normal” values
        ])
        logging.info("⧴ Handover delay completed, restored original qdisc settings")

def handle_handover_command(payload: Dict[str, Any], ho_delay_ms: float) -> None:
    global status, current_dev, new_dev

    if status != "registered":
        logging.warning("⚠️ Received handover_command while not in registered state, ignoring.")
        return

    grd_id_recv = payload["grd_id"]
    if grd_id_recv != grd_id:
        logging.warning(f"⚠️ Received handover_command for grd {grd_id_recv} while current grd is {grd_id}, ignoring.")
        return                  
    
    upstream_sids = payload["sids"]                  # new sid sequence for the user (e.g., "2001:db8:200::1")
    new_sat_ipv6 = upstream_sids.split(",")[0]          # first SID is the new satellite to reach the grd.
    new_dev = derive_egress_dev(new_sat_ipv6)  # derive the egress dev to reach the grd via the new satellite
    
    threading.Thread(
            target=traffic_pause,
            args=(ho_delay_ms,),
            daemon=True,
            name="handover-traffic-pause",
    ).start()
    
    if new_dev != current_dev:
        current_dev = new_dev
        if not wait_for_link_local_via_route(new_sat_ipv6, timeout_s=link_setup_delay_s):
            logging.warning(
                f"⚠️ No route with link-local next-hop for {new_sat_ipv6} before handover command timeout window."
            )

    # update route to grd via new satellite
    ip_cmd = build_srv6_route_replace(grd_ipv6, upstream_sids, new_dev)
    run_cmd(ip_cmd)
    # add new default route
    default_prefix = "default"
    ip_cmd = build_srv6_route_replace(default_prefix, upstream_sids, new_dev)
    run_cmd(ip_cmd)

    # status update and logging
    status = "registered"
    new_dev_sat_name = links_db.get(new_dev, {}).get("remote_endpoint_name", "unknown")

    if payload.get("type") == "handover_command":
        logging.info(f"📡 Handover command received by {grd_id} with upstream SIDs {upstream_sids} through satellite {new_dev_sat_name}")
        write_report_event("handover", "user", grd_id, new_dev_sat_name)

    send_handover_complete_udp(
        grd_ipv6=grd_ipv6,
        grd_port=grd_port,
        user_dev=current_dev,
        user_ipv6=local_ipv6,
        upstream_sids=upstream_sids,
    )
    logging.info(f"✉️ Sent handover complete to {grd_id}")

def handle_registration_accept(payload: Dict[str, Any]) -> None:
    global status, current_dev, heartbeat_failures

    if status != "registration_in_progress":
        logging.debug("⚠️ Received registration_accept while not in registration_in_progress state, ignoring.")
        return

    grd_id_recv = payload["grd_id"]                      
    upstream_sids = payload["sids"]                  # new sid sequence for the user (e.g., "2001:db8:200::1")
    init_sat_ipv6_recv = upstream_sids.split(",")[0]         # first SID is the new egress SID to reach the grd. Shall be the IP address of a connected sat
    init_sat_dev_recv = derive_egress_dev(init_sat_ipv6_recv)  # derive the egress dev to reach the grd via the initial satellite
    if grd_id_recv != grd_id:
        logging.warning(f"⚠️ Received registration_accept from {grd_id_recv} while current grd is {grd_id}, ignoring.")
        return
    if init_sat_ipv6_recv != links_db.get(current_dev, {}).get("remote_endpoint_ipv6", ""):
        logging.warning(f"⚠️ Received registration_accept with initial satellite {init_sat_ipv6_recv} different from expected {links_db.get(current_dev, {}).get('remote_endpoint_ipv6', '')}, ignoring.")
        return
    if links_db.get(current_dev, {}).get("status", None) != "available":
        logging.warning(f"⚠️ Received registration_accept with initial satellite {init_sat_ipv6_recv} whose link is not available according to links_db, ignoring.")
        return
    
    current_dev = init_sat_dev_recv
    ip_cmd = build_srv6_route_replace(grd_ipv6, upstream_sids, current_dev)
    if not wait_for_link_local_via_route(init_sat_ipv6_recv, timeout_s=link_setup_delay_s):
        logging.warning(
            f"⚠️ No route with link-local next-hop for {init_sat_ipv6_recv} before handover request timeout window."
        )
    run_cmd(ip_cmd)
    
    # add ipv6 default route via grd_ipv6
    default_prefix = "default"
    ip_cmd = build_srv6_route_replace(default_prefix, upstream_sids, current_dev)
    run_cmd(ip_cmd)
    cancel_registration_timeout()
    with heartbeat_lock:
        heartbeat_failures = 0
    status = "registered"
    remote_endpoint = links_db.get(current_dev, {}).get("remote_endpoint_name", "unknown")
    logging.info(f"📡 Registration accepted by {grd_id} with with upstream SIDs {upstream_sids} via satellite {remote_endpoint}")
    write_report_event("registration", "_", grd_id, remote_endpoint)

def handle_hello(payload: Dict[str, Any]) -> None:
    global heartbeat_failures
    grd_id_recv = payload.get("grd_id", "")
    if grd_id_recv and grd_id_recv != grd_id:
        logging.debug("Ignoring HELLO from unexpected GRD %s", grd_id_recv)
        return
    with heartbeat_lock:
        heartbeat_failures = 0

def handle_command(payload: Dict[str, Any], ho_delay_ms: float) -> None:
    if payload.get("type") == "handover_command":
        handle_handover_command(payload, ho_delay_ms)
        return
    elif payload.get("type") == "registration_accept":
        handle_registration_accept(payload)
    elif payload.get("type") == "hello":
        handle_hello(payload)
    else:
        raise ValueError(f"Unsupported command type: {payload.get('type')}")


# ----------------------------
#   WATCHERS
# ----------------------------
def watch_link_actions_loop () -> None:
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
                    remote_endpoint = l.get("endpoint1") if l.get("endpoint2") == node_name else l.get("endpoint2")
                    if link_dev not in links_db:
                        remote_endpoint_ipv6 = resolve_ipv6_from_hosts(remote_endpoint) if remote_endpoint else ""
                        logging.info(f"➕ Detected new satellite {remote_endpoint}")
                        update_links_db(link_dev=link_dev, etcd_link_data=l, last_created=time.time(), last_updated=time.time(), status="available", remote_endpoint_ipv6=remote_endpoint_ipv6)
                    elif links_db[link_dev].get("status") == "available":
                            logging.debug(f"🔄 Detected update for existing link of satellite {remote_endpoint}")
                            update_links_db(link_dev=link_dev, etcd_link_data=l, last_updated=time.time(), status="available")
                    elif links_db[link_dev].get("status") == "unavailable":
                            logging.info(f"🔁 Detected re-appearance of previous satellite {remote_endpoint}")
                            update_links_db(link_dev=link_dev, etcd_link_data=l, last_created=time.time(), last_updated=time.time(), status="available")
                    if "expected_duration" in l:
                        # expected_duration is expressed in seconds by the epoch annotation tool; null falls back to the default initial duration.
                        expected_duration = parse_expected_duration(l["expected_duration"])
                        update_links_db(link_dev=link_dev, last_duration=expected_duration)
                        logging.debug(f"⏱️ Updated expected duration for satellite {remote_endpoint} to {expected_duration} seconds based on link metadata")
                    if status == "not_registered":
                        handle_registration_request()

                elif isinstance(event, etcd3.events.DeleteEvent):
                    # update link_db
                    deleted_dev = event.key.decode().split("/")[-1]
                    remote_endpoint = links_db.get(deleted_dev, {}).get("remote_endpoint_name", "")
                    logging.info(f"➖ Detected out of range for satellite {remote_endpoint}")
                    last_duration = time.time() - links_db.get(deleted_dev, {}).get("last_created", time.time())
                    update_links_db(link_dev=deleted_dev, last_updated=time.time(), status="unavailable", last_duration=last_duration)
                    if deleted_dev == current_dev:
                        logging.warning(f"⚠️ Current access satellite {remote_endpoint} out of range, resetting state and re-registering.")
                        status = "not_registered"
                        current_dev = None
                        new_dev = None
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


# ----------------------------
#   SERVER
# ----------------------------
def serve(bind_addr: str, port: int, ho_delay: float) -> None:
    sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    sock.bind((bind_addr, port))
    logging.info("⚙️ usr_agent listening on [%s]:%d", bind_addr, port)

    while True:
        data, peer = sock.recvfrom(MAX_UDP_RECV_BYTES)
        try:
            msg = json.loads(data.decode("utf-8"))
            logging.debug(f"📩 Received command from {peer}: {msg}")
            # ensure grd_id is in grd_list and prepare qdisc for it if it's a new grd_id (e.g., in case of multiple GRDs or if the same GRD serves multiple users with different grd_ids)
            if "grd_id" not in msg:
                logging.warning("⚠️ Received command without grd_id from %s, ignoring.", peer)
                continue
            if grd_id not in grd_list:
                # add new grd to grd list and prepare qdisc for them
                logging.info(f" ⚠️ Received command for ground station {grd_id} not in the list, ignoring")
                continue
            handle_command(msg, ho_delay_ms=ho_delay)
        except Exception as e:
            logging.warning("❌ Failed command from [%s]:%d: %s", peer[0], peer[1], e)


# ----------------------------
#   ENTRYPOINT
# ----------------------------
def main() -> None:
    global chose_reg_device, grd_ipv6, grd_port, grd_id, user_callback_port, local_ipv6, etcd_client, link_setup_delay_s, reg_metadata, registration_accept_timeout_s, handover_command_timeout_s, link_duration_initial_value_s, heartbeat_interval_s, heartbeat_max_failures, report_file, measurement_top_n_links, select_measurement_report_links
    
    ap = argparse.ArgumentParser()
    ap.add_argument("--bind", default="::")
    ap.add_argument("--port", type=int, default=5006, help="UDP port where usr1 listens for handover_command")
    ap.add_argument("--handover-delay", type=float, help="Handover delay in mseconds (requires veth0_rt, use app/shaping-ns-create-v6.sh)", default=0)
    ap.add_argument("--grd", type=str, default=None, help="Name of ground station (e.g., grd1)")
    ap.add_argument("--grd-port", type=int, default=5005, help="UDP port on serving ground station (default: 5005)")
    ap.add_argument("--registration-timeout", type=float, default=3.0, help="seconds to wait for registration_accept before retrying registration")
    ap.add_argument("--link-setup-delay", type=float, default=3, help="Estimated time in seconds needed by to setup relevat routes and interfaces after link creatio, default 5s)")
    ap.add_argument("--link-duration-initial-value", type=float, default=4*60, help="Initial value in seconds for the duration of new links, default: 4min)")
    ap.add_argument("--measurement-top-n-links-strategy", default="lifetime", help="Strategy selecting best available links to include in measurement reports in addition to current_dev (default : lifetime). Supported values: lifetime, delay")
    ap.add_argument("--measurement-top-n-links", type=int, default=16, help="Number of best available links to include in measurement reports in addition to current_dev (default: 16)")
    ap.add_argument("--report", action="store_true", help="Enable detailed reporting of internal state for debugging")
    ap.add_argument("--log-level", default="INFO", help="Logging level (e.g., DEBUG, INFO, WARNING)")
    args = ap.parse_args()
    
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(message)s")
    
    etcd_client = get_etcd_client()

    refresh_hosts_ipv6_cache()
    
    local_ipv6 = resolve_ipv6_from_hosts(os.environ["NODE_NAME"])

    grd_id = args.grd
    if grd_id is None:
        logging.info("⚠️ No ground station information provided, trying to derive with geo proximity")
        grd_id = chose_proximity_grd()
        if grd_id is None:
            logging.error("❌ Failed to derive ground station with proximity strategy. Please provide ground station address with --grd.")
            sys.exit(1)
        else:
            logging.info(f"📡 Derived ground station {grd_id} with proximity strategy.")
    # resolve grd_address if it's a hostname
    try:
        grd_ipv6 = resolve_ipv6_from_hosts(grd_id)
        if not grd_ipv6:
            logging.error(f"❌ Failed to resolve IPV6 address for ground station {grd_id}. Please check /etc/hosts entries.")
            sys.exit(1)
    except Exception as e:
        logging.error(f"❌ Failed to resolve IPV6 address for ground station {grd_id}: {e}")
        sys.exit(1)
    
    # write grd_id and grd_ipv6 to a the "grd_config" file to be used by other components (e.g., for testing and validation purposes)
    try:
        with open("/app/grd_config", "w") as f:
            f.write(f"{grd_ipv6} {grd_id}\n")
    except Exception as e:
        logging.error(f"❌ Failed to write grd_config file: {e}")
        sys.exit(1) 

    # prepare qdisc for handover delay if configured (requires veth0_rt to be present, which is created by shaping-ns-create-v6.sh script)
    if subprocess.run(
        ["ip", "link", "show", "veth0_rt"],
        text=True,
        capture_output=True,
    ).returncode != 0:
        logging.info("veth0_rt interface not found, creating shaping namespace for handover delay")
        run_cmd(["/app/extra/QoS/shaping-ns-create-v6.sh"])
    
    init_qdisc()
    grd_list.append(grd_id)
    prepare_qdisc_for_grd(grd_ipv6=grd_ipv6, grd_id=grd_id)

    grd_port = args.grd_port
    user_callback_port = args.port
    registration_accept_timeout_s = args.registration_timeout
    link_setup_delay_s = args.link_setup_delay
    link_duration_initial_value_s = args.link_duration_initial_value
    measurement_top_n_links = args.measurement_top_n_links + 1  # +1 to account for current_dev which is always included in the report
    if args.measurement_top_n_links_strategy == "lifetime":
        chose_reg_device = lifetime_strategy
        select_measurement_report_links = select_measurement_report_links_grid_lifetime
    elif args.measurement_top_n_links_strategy == "delay":
        chose_reg_device = lifetime_strategy
        select_measurement_report_links = select_measurement_report_links_grid_delay
    else:
        logging.error(f"❌ Link selection strategy {args.measurement_top_n_links_strategy} not supported")
        sys.exit(1) 
    reg_metadata = {}

    if args.report:
        report_file_name = f"report_{os.environ['NODE_NAME']}_conn_manager_usr.log"
        report_file = open(report_file_name, "w")
        logging.info(f"📊 Detailed reporting enabled, writing to {report_file_name}")

    preload_satellite_nodes_db_from_etcd()
    preload_links_db_from_etcd()
    
    handle_registration_request()

    threading.Thread(
        target=watch_link_actions_loop,
        daemon=True,
        name="watch-link-actions",
    ).start()

    threading.Thread(
        target=reporting_loop,
        daemon=True,
        name="processing-handover",
    ).start()
    threading.Thread(
        target=heartbeat_loop,
        daemon=True,
        name="heartbeat-loop",
        ).start()
    
    serve(bind_addr=args.bind, port=args.port, ho_delay=args.handover_delay)

if __name__ == "__main__":
    main()
