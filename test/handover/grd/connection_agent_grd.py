#!/usr/bin/env python3
import argparse
from asyncio import log
from email.policy import default
import json
import logging
import os
import random
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
KEY_NODES_PREFIX = "/config/nodes/"
link_setup_delay_s = 0.2 # estimated time needed by sat-agent to setup relevat routes and interfaces after a link is added in etcd, used to delay registration after link event to increase chances that the link is fully setup in the sat-agent before registration attempt (which can reduce registration failures due to missing routes/interfaces in the sat-agent at the time of registration)
DEV_RE = re.compile(r"\bdev\s+([^\s]+)\b")
VIA_RE = re.compile(r"\bvia\s+([^\s]+)\b")
SEGS_RE = re.compile(r"\bsegs\s+\d+\s+\[\s*([^\]]+)\]")
user_db: Dict[str, Dict[str, str]] = {} # key: user_id, value: {"upstream_sids": str, "downstream_sids": str, "dev": str} (for tracking registered users and their current routes)
links_db: Dict[str, Dict[str, str]] = {}  # key dev_id, value: {"endpoint1": str, "endpoint2": str, ...}
satellite_nodes_db: Dict[str, Dict[str, Any]] = {}
link_duration_initial_value_s = 4*60  # initial value for link duration (sec)
max_links = 1024  # max number of simultaneous links (can be tuned based on expected number of available links and resource constraints)
process_connection_handover = None  # assign the function to process the set of links/devs to be used for connecting to the satellite network
handover_metadata = {}  # metadata dict to pass to the handover strategy function (can include threshold values, weights, or other parameters needed for the strategy logic)
handover_periodic_check_s = 3.3  # periodic check interval for handover decision (can be tuned based on expected link dynamics and handover time requirements)
handover_delay_ms = 0.0
user_handover_filters: List[str] = []  # filters to apply in the exact provided sequence for user handover decisions
grd_handover_filters: List[str] = []  # filters to apply in the exact provided sequence for grd handover decisions
handover_hold_period_s = 15.0  # hold period after a handover during which no new handover is allowed, to avoid too frequent handovers in case of rapidly changing link conditions. 

hosts_ipv6_cache: Dict[str, str] = {}
grd_ipv6 = ""
is_walker_star = False
user_callback_port = 5006
heartbeat_interval_s = 1.0
heartbeat_max_failures = 3
heartbeat_failures: Dict[str, int] = {}
heartbeat_lock = threading.Lock()
_UNSET = object() # sentinel value to distinguish between "no update" and "update with None/empty" in db update functions
MAX_UDP_RECV_BYTES = 65535 # max size of UDP payload to receive for user callbacks, can be tuned based on expected message size and memory constraints
report_file = None  # file handle for detailed report output, if enabled by args
USER_STATUS_REGISTERED = "registered"
USER_STATUS_HANDOVER_IN_PROGRESS = "handover_in_progress"
HANDOVER_SHUFFLE_SEED = int(os.getenv("HANDOVER_SHUFFLE_SEED", "1973"))
handover_shuffle_rng = random.Random(HANDOVER_SHUFFLE_SEED)
COMPACT_LINK_REPORT_FMT = "cldb1"
STATUS_CODE_TO_NAME = {
    0: "unavailable",
    1: "available",
    2: "connected",
}

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

def decode_user_links_db(payload_user_links_db: Any) -> Dict[str, Dict[str, Any]]:
    links_db_payload = payload_user_links_db
    if isinstance(links_db_payload, str):
        links_db_payload = json.loads(links_db_payload) if links_db_payload else {}
    if not isinstance(links_db_payload, dict):
        return {}

    if links_db_payload.get("_fmt") != COMPACT_LINK_REPORT_FMT:
        return links_db_payload

    decoded_links_db: Dict[str, Dict[str, Any]] = {}
    for row in links_db_payload.get("r", []):
        if not isinstance(row, list) or len(row) < 9:
            continue
        link_dev = row[0]
        if not isinstance(link_dev, str) or not link_dev:
            continue
        status = STATUS_CODE_TO_NAME.get(row[1], "unavailable")
        decoded_links_db[link_dev] = {
            "status": status,
            "last_duration": row[2],
            "last_created": row[3],
            "rate": row[4],
            "delay": row[5],
            "loss": row[6],
            "remote_endpoint_ipv6": row[7],
            "remote_endpoint_name": row[8],
        }
    return decoded_links_db


def update_user_db(user_id: str, user_ipv6: Any = _UNSET, upstream_sids: Any = _UNSET, downstream_sids: Any = _UNSET, grd_dev: Any = _UNSET, status: Any = _UNSET, user_links_db: Any = _UNSET, txid: Any = _UNSET, user_dev: Any = _UNSET, qdisc_minor: Any = _UNSET, last_handover: Any = _UNSET) -> None:
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
        "last_handover": last_handover if last_handover is not _UNSET else user_db.get(user_id, {}).get("last_handover", None),
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


def parse_handover_filters(raw_filters: str, accepted_filters: set[str], label: str) -> List[str]:
    # Preserve comma-separated filter order exactly as provided (except surrounding whitespace).
    parsed_filters = [f.strip() for f in raw_filters.split(",")]
    if any(not f for f in parsed_filters):
        raise ValueError(
            f"Invalid {label} handover filters: empty filter found in '{raw_filters}'."
        )
    invalid_filters = [f for f in parsed_filters if f not in accepted_filters]
    if invalid_filters:
        raise ValueError(
            f"Invalid {label} handover filter(s): {invalid_filters}. Accepted values are: {sorted(accepted_filters)}"
        )
    return parsed_filters


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


def get_handover_threshold_s(metadata: dict, last_duration: Any = None) -> float:
    threshold_s = metadata.get("threshold_s", link_duration_initial_value_s/4.0)
    if last_duration is not None:
        threshold_s = min(threshold_s, float(last_duration) / 4.0)
    return threshold_s


def is_user_active(user_id: str) -> bool:
    return user_db.get(user_id, {}).get("status") in {USER_STATUS_REGISTERED, USER_STATUS_HANDOVER_IN_PROGRESS}


def get_grd_dev_user_count(dev: str, exclude_user_id: str | None = None) -> int:
    return sum(
        1
        for uid, user_info in user_db.items()
        if uid != exclude_user_id and is_user_active(uid) and user_info.get("grd_dev") == dev
    )


def get_satellite_orbit_num(sat_name: str) -> Tuple[int,int]:
    satellite_metadata = satellite_nodes_db.get(sat_name, {})
    sat_config = satellite_metadata.get("sat_config", {})
    sat_orbit = sat_config.get("sat_orbit")
    sat_num_in_orbit = sat_config.get("sat_num_in_orbit")
    if sat_orbit is None or sat_num_in_orbit is None:
        raise RuntimeError(f"Satellite '{sat_name}' has no sat_orbit or sat_num_in_orbit metadata")
    return int(sat_orbit),int(sat_num_in_orbit)


def get_orbit_distance_between_satellites(sat_name_a: str, sat_name_b: str) -> int:
    sat_orbit_a, sat_num_in_orbit_a = get_satellite_orbit_num(sat_name_a)
    sat_orbit_b, sat_num_in_orbit_b = get_satellite_orbit_num(sat_name_b)

    # compute distance in number of orbits between the two satellites, considering wrap-around and walker star configuration
    min_sat_orbit = sat_orbit_a if sat_orbit_a < sat_orbit_b else sat_orbit_b
    max_sat_orbit = sat_orbit_a if sat_orbit_a >= sat_orbit_b else sat_orbit_b

    distance_1 = max_sat_orbit - min_sat_orbit
    shell_config_a = satellite_nodes_db.get(sat_name_a, {}).get("shell_config", {})
    shell_config_b = satellite_nodes_db.get(sat_name_b, {}).get("shell_config", {})
    total_orbits = shell_config_a.get("number_of_orbit", shell_config_b.get("number_of_orbit"))
    if total_orbits is None:
        raise RuntimeError(
            f"Satellites '{sat_name_a}' and '{sat_name_b}' have no number_of_orbit metadata"
        )
    total_orbits = int(total_orbits)
    distance_2 = total_orbits - max_sat_orbit + min_sat_orbit

    if not is_walker_star:
        orbit_distance = min(distance_1, distance_2)
    else:
        orbit_distance = distance_1
    # compute in-orbit ISLs
    min_sat_num_in_orbit_a = sat_num_in_orbit_a if sat_num_in_orbit_a < sat_num_in_orbit_b else sat_num_in_orbit_b
    max_sat_num_in_orbit_b = sat_num_in_orbit_a if sat_num_in_orbit_a >= sat_num_in_orbit_b else sat_num_in_orbit_b
    total_sats_in_orbit = shell_config_a.get("number_of_satellite_per_orbit", shell_config_b.get("number_of_satellite_per_orbit"))
    if total_sats_in_orbit is None:
        raise RuntimeError(
            f"Satellites '{sat_name_a}' and '{sat_name_b}' have no number_of_satellite_per_orbit metadata"
        )
    total_sats_in_orbit = int(total_sats_in_orbit)
    in_orbit_distance_1 = max_sat_num_in_orbit_b - min_sat_num_in_orbit_a
    in_orbit_distance_2 = total_sats_in_orbit - max_sat_num_in_orbit_b + min_sat_num_in_orbit_a
    in_orbit_distance = min(in_orbit_distance_1, in_orbit_distance_2)
    
    return orbit_distance + in_orbit_distance


def filter_devs_min_duration(
    user_id: str,   
    candidate_devs: List[Tuple[str, Dict[str, Any]]],
    min_remaining_duration_s: float,
    candidate_type: str,
) ->  List[Tuple[str, Dict[str, Any]]]:
    now = time.time()
    new_candidate_devs = []
    for dev, link in candidate_devs:
        remaining_duration = link.get("last_duration", 0) - (now - link.get("last_created", 0))
        if remaining_duration >= min_remaining_duration_s:
            new_candidate_devs.append((dev, link))
    if not new_candidate_devs:
        # if no candidate dev has remaining duration above threshold, keep all candidates for the next selection step (which can be based on other criteria like load balancing or orbit distance)
        new_candidate_devs = list(candidate_devs)
        logging.warning(f"⚠️ No {candidate_type} links for user {user_id} has remaining duration above threshold of {min_remaining_duration_s:.1f}s, keeping all candidates for next selection step")
    return new_candidate_devs

def filter_devs_min_duration_coupled(
    user_id: str,   
    access_sat_couples: List[Tuple[str, str]],
    min_remaining_duration_s: float,
) ->  List[Tuple[str, str]]:
    now = time.time()
    new_access_sat_couples = []
    user_dev_db = user_db.get(user_id, {}).get("user_links_db", {}) or {}
    grd_dev_db = links_db
    for c_grd_dev, c_user_dev in access_sat_couples:
        remaining_duration_grd_dev = grd_dev_db.get(c_grd_dev, {}).get("last_duration", 0) - (now - grd_dev_db.get(c_grd_dev, {}).get("last_created", 0))
        remaining_duration_user_dev = user_dev_db.get(c_user_dev, {}).get("last_duration", 0) - (now - user_dev_db.get(c_user_dev, {}).get("last_created", 0))
        if remaining_duration_grd_dev > min_remaining_duration_s and remaining_duration_user_dev > min_remaining_duration_s and (c_grd_dev, c_user_dev) not in new_access_sat_couples:
            new_access_sat_couples.append((c_grd_dev, c_user_dev))
    if not new_access_sat_couples:
        logging.warning(f"⚠️ No couple access links for user {user_id} has remaining duration above threshold of {min_remaining_duration_s:.1f}s, keeping all candidates for next selection step")
        return list(access_sat_couples)
    return new_access_sat_couples

def filter_devs_min_orbit_hops(
    user_id: str,
    candidate_devs: List[Tuple[str, Dict[str, Any]]],
    other_endpoint_dev: str,
    candidate_type: str,
    tolerance: int = 2,
) -> List[Tuple[str, Dict[str, Any]]]:
    # find minimum orbit hops
    endpoint_dev = other_endpoint_dev 
    if candidate_type == "grd":
        user_links_db = user_db.get(user_id, {}).get("user_links_db") or {}
        endpoint_name = user_links_db.get(endpoint_dev, {}).get("remote_endpoint_name")
    elif candidate_type == "user":
        endpoint_name =links_db.get(endpoint_dev, {}).get("remote_endpoint_name")
    if endpoint_name:
        best_orbit_hops = min(
            get_orbit_distance_between_satellites(link.get("remote_endpoint_name"), endpoint_name)
            for dev, link in candidate_devs
            if link.get("remote_endpoint_name")
        )
        new_candidate_devs = [
            (dev, link)
            for dev, link in candidate_devs
            if link.get("remote_endpoint_name") and get_orbit_distance_between_satellites(link.get("remote_endpoint_name"), endpoint_name) <= best_orbit_hops + tolerance
        ]
        logging.debug(f"🔎 Candidate {candidate_type} links for user {user_id} after filtering by min orbit hops ({best_orbit_hops}): {list(dev for dev, _ in new_candidate_devs)}")
        return new_candidate_devs
    else:
        logging.warning(f"⚠️ Could not determine endpoint satellite for user {user_id} to filter candidates by orbit hops, skipping this filter")
        return list(candidate_devs)

def filter_devs_min_orbit_hops_coupled(
    user_id: str,   
    access_sat_couples: List[Tuple[str, str]],
    min_remaining_duration_s: float,
    tolerance: int = 2,
) -> List[Tuple[str, str]]:
    # find minimum orbit hops
    new_access_sat_couples = []
    user_links_db = user_db.get(user_id, {}).get("user_links_db", {}) or {}
    grd_links_db = links_db
    best_orbit_hops = 1024 # initialize with a large number to be sure to find the minimum among the candidates
    access_sat_couples_hops= {}
    for c_grd_dev, c_user_dev in access_sat_couples:
        orbit_hops = get_orbit_distance_between_satellites(user_links_db.get(c_user_dev, {}).get("remote_endpoint_name"), grd_links_db.get(c_grd_dev, {}).get("remote_endpoint_name"))
        if orbit_hops < best_orbit_hops:
            best_orbit_hops = orbit_hops
        access_sat_couples_hops[(c_grd_dev, c_user_dev)] = orbit_hops
    
    for c_grd_dev, c_user_dev in access_sat_couples:
        if access_sat_couples_hops.get((c_grd_dev, c_user_dev), 1024) <= best_orbit_hops + tolerance + 1e-6 and (c_grd_dev, c_user_dev) not in new_access_sat_couples:
            new_access_sat_couples.append((c_grd_dev, c_user_dev))  

    if not new_access_sat_couples:
        logging.warning(f"⚠️ Could not determine endpoint satellite for user {user_id} to filter candidates by orbit hops, skipping this filter")
        return list(access_sat_couples)
    logging.debug(f"🔎 Candidate access links for user {user_id} after filtering by min orbit hops {best_orbit_hops}: {new_access_sat_couples}")
    return new_access_sat_couples

def filter_devs_load_balancing(
    user_id: str,
    candidate_devs: List[Tuple[str, Dict[str, Any]]],
    tolerance: float,
    candidate_type: str,
) -> List[Tuple[str, Dict[str, Any]]]:
    # find minimum user count among candidate devs
    if candidate_type == "grd":
        user_dev = user_db.get(user_id, {}).get("user_dev")
        user_links_db = user_db.get(user_id, {}).get("user_links_db") or {}
        user_sat_name = user_links_db.get(user_dev, {}).get("remote_endpoint_name")
    elif candidate_type == "user":
        logging.error("❌ Load balancing filter should not be applied to user link candidates, skipping")
        return list(candidate_devs)
    min_user_count = min(
        get_grd_dev_user_count(dev, exclude_user_id=user_id)
        for dev, link in candidate_devs
    )
    new_candidate_devs = [
        (dev, link)
        for dev, link in candidate_devs
        if ((user_count := get_grd_dev_user_count(dev, exclude_user_id=user_id)) == min_user_count or user_count <= tolerance + 1e-6)
    ]
    logging.debug(f"🔎 Candidate {candidate_type} links for user {user_id} after filtering by load balancing (min user count {min_user_count}): {list(dev for dev, _ in new_candidate_devs)}")
    return new_candidate_devs

def filter_devs_longest_duration_coupled(
    user_id: str,   
    access_sat_couples: List[Tuple[str, str]],
    tolerance: float = 0,
) -> List[Tuple[str, str]]:
    # find minimum orbit hops
    new_access_sat_couples = []
    user_links_db = user_db.get(user_id, {}).get("user_links_db", {}) or {}
    grd_links_db = links_db
    remaining_duration_couples = {}
    max_remaining_duration = 0.0 # initialize with 0 to be sure to find the maximum among the candidates (which should be above the threshold to be selected)
    now = time.time()
    for c_grd_dev, c_user_dev in access_sat_couples:
        remaining_duration_grd_dev = grd_links_db.get(c_grd_dev, {}).get("last_duration", 0) - (now - grd_links_db.get(c_grd_dev, {}).get("last_created", 0))
        remaining_duration_user_dev = user_links_db.get(c_user_dev, {}).get("last_duration", 0) - (now - user_links_db.get(c_user_dev, {}).get("last_created", 0))
        remaining_duration_couples[(c_grd_dev, c_user_dev)] = min(remaining_duration_grd_dev, remaining_duration_user_dev)
        if remaining_duration_couples[(c_grd_dev, c_user_dev)] > max_remaining_duration:
            max_remaining_duration = remaining_duration_couples[(c_grd_dev, c_user_dev)]
    if max_remaining_duration>0:
        new_access_sat_couples = [
            (c_grd_dev, c_user_dev)
            for (c_grd_dev, c_user_dev), remaining_duration in remaining_duration_couples.items()
            if remaining_duration >= max_remaining_duration - tolerance - 1e-6 # use a small tolerance to avoid floating point comparison issues
        ]
    if not new_access_sat_couples:
        logging.warning(f"⚠️ No couple access links for user {user_id} passed longest-duration filtering, keeping all candidates for next selection step")
        new_access_sat_couples = access_sat_couples
    else:
        logging.debug(f"🔎 Candidate access links for user {user_id} after filtering by longest remaining duration ({max_remaining_duration:.1f}s): {new_access_sat_couples}")
    return new_access_sat_couples


def filter_devs_longest_duration(
    user_id: str,
    candidate_devs: List[Tuple[str, Dict[str, Any]]],
    candidate_type: str,
) -> List[Tuple[str, Dict[str, Any]]]:
    now = time.time()
    remaining_by_dev = {
        dev: link.get("last_duration", 0) - (now - link.get("last_created", 0))
        for dev, link in candidate_devs
    }
    max_remaining_duration = max(remaining_by_dev.values())
    new_candidate_devs = [
        (dev, link)
        for dev, link in candidate_devs
        if remaining_by_dev[dev] >= max_remaining_duration - 1e-6 # use a small tolerance to avoid floating point comparison issues
    ]
    logging.debug(f"🔎 Candidate {candidate_type} links for user {user_id} after filtering by max remaining duration ({max_remaining_duration:.1f}s): {list(dev for dev, _ in new_candidate_devs)}")
    return new_candidate_devs

def filter_devs_min_delay(
    user_id: str,
    candidate_devs: List[Tuple[str, Dict[str, Any]]],
    candidate_type: str,
) -> List[Tuple[str, Dict[str, Any]]]:
    now = time.time()
    delay_by_dev = {
        dev: parse_delay(link.get("delay", 0))
        for dev, link in candidate_devs
    }
    min_delay = min(delay_by_dev.values())
    new_candidate_devs = [
        (dev, link)
        for dev, link in candidate_devs
        if delay_by_dev[dev] <= min_delay + 1e-6 # use a small tolerance to avoid floating point comparison issues
    ]
    logging.debug(f"🔎 Candidate {candidate_type} links for user {user_id} after filtering by min delay ({min_delay:.1f}ms): {list(dev for dev, _ in new_candidate_devs)}")
    return new_candidate_devs

def filter_devs_min_delay_coupled(
    user_id: str,   
    access_sat_couples: List[Tuple[str, str]],
    tolerance: float = 0,
) -> List[Tuple[str, str]]:
    # find minimum orbit hops
    new_access_sat_couples = []
    user_links_db = user_db.get(user_id, {}).get("user_links_db", {}) or {}
    grd_links_db = links_db
    min_delay_couples = {}
    best_delay = None
    now = time.time()
    for c_grd_dev, c_user_dev in access_sat_couples:
        delay_grd_dev = parse_delay(grd_links_db.get(c_grd_dev, {}).get("delay", 0))
        delay_user_dev = parse_delay(user_links_db.get(c_user_dev, {}).get("delay", 0))
        couple_delay = max(delay_grd_dev, delay_user_dev) # the couple delay is the maximum delay among the user and grd dev to avoid selecting couples with one of the two links having a very high delay even if the other link has a low delay
        min_delay_couples[(c_grd_dev, c_user_dev)] = couple_delay
        if best_delay is None or couple_delay < best_delay:
            best_delay = couple_delay
    if best_delay is not None:
        new_access_sat_couples = [
            (c_grd_dev, c_user_dev)
            for (c_grd_dev, c_user_dev), couple_delay in min_delay_couples.items()
            if couple_delay <= best_delay + tolerance + 1e-6 # use a small tolerance to avoid floating point comparison issues
        ]
    if not new_access_sat_couples:
        logging.warning(f"⚠️ No couple access links for user {user_id} passed min-delay filtering, keeping all candidates for next selection step")
        new_access_sat_couples = access_sat_couples
    else:
        logging.debug(f"🔎 Candidate access links for user {user_id} after filtering by minimum delay ({best_delay:.1f}ms): {new_access_sat_couples}")
    return new_access_sat_couples

# ----------------------------
#   MAIN LOGIC FOR LINK MANAGEMENT LOCAL SIDE
# ----------------------------
def preload_satellite_nodes_db_from_etcd(etcd_client) -> None:
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
                if "expected_duration" in l:
                    expected_duration = parse_expected_duration(l["expected_duration"])
                    update_link_db(link_dev=link_dev, last_duration=expected_duration)
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
            logging.debug(f"🔄 Ground station detected update for link with satellite {remote_endpoint}")
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
            logging.info(f"⚠️ Ground station using out of range satellite {sat_name} for user {user_id}, evaluating rescue handover...")
            update_user_db(user_id=user_id, grd_dev=None)  # Update user_db with new status for the user during local handover processing
            new_user_dev = user_info.get("user_dev")
            new_grd_dev, needed = user_handover_strategy(user_id, handover_metadata, new_user_dev, user_handover_filters)
            if needed:
                sat_name = links_db.get(new_grd_dev, {}).get("remote_endpoint_name", "unknown")
                start_process_user_handover_thread(user_id = user_id, new_grd_dev = new_grd_dev, new_user_dev = new_user_dev)
            else:
                logging.warning(f"⚠️ No available link found for {user_id} after deletion of {sat_name}, {user_id} is now without a grd link")

def user_handover_strategy(user_id: str, metadata: dict, new_grd_dev: str, handover_filters: list) -> Tuple[str, bool]:
    user_dev = user_db.get(user_id, {}).get("user_dev", None)
    if new_grd_dev==None:
        new_grd_dev = user_db.get(user_id, {}).get("grd_dev", None)
    user_links_db = user_db.get(user_id, {}).get("user_links_db") or {}
    link_status_acceptable = "available"  # for user dev we consider all available links as candidates for handover
    user_dev_last_duration = user_links_db.get(user_dev, {}).get("last_duration", None)
    user_last_handover = user_db.get(user_id, {}).get("last_handover")
    min_hops_tolerance = metadata.get("min_hops_tolerance", 0)  # tolerance in number of hops for min orbit hops filter
    if user_last_handover is None:
        user_last_handover = 0.0
    
    if user_dev == None:
        # no link currently assigned to user, so handover is needed to assign the best connected link
        remaining_duration = 0
    elif user_links_db.get(user_dev,{}).get("status",None) != link_status_acceptable:
            # current link is no more connected (grd) or available (user), so handover is needed to assign the best connected link
        remaining_duration = 0
    else:
        remaining_duration = user_links_db.get(user_dev, {}).get("last_duration", 0) - (time.time() - user_links_db.get(user_dev, {}).get("last_created", 0))
    
    threshold_s = get_handover_threshold_s(metadata, user_dev_last_duration)  # threshold for minimum remaining duration to consider a handover
    
    current_ok = (
        user_dev is not None
        and user_links_db.get(user_dev, {}).get("status") == link_status_acceptable
    )
    if current_ok and remaining_duration > threshold_s and time.time() - user_last_handover < handover_hold_period_s:
        # no handover while current link is healthy and hold period is still active
        logging.debug(f"✅ Current user link {user_dev} for user {user_id} has remaining duration {remaining_duration:.1f}s and threshold {threshold_s:.1f}s, sec since last handover {time.time() - user_last_handover:.1f}s and handover_hold_period_s {handover_hold_period_s:.1f}s, no handover needed")
        return user_dev, False
    
    logging.debug(f"⏱️ Evaluating user handover for user {user_id} on current user link {user_dev} with remaining duration {remaining_duration:.1f}s and threshold {threshold_s:.1f}s")
    candidate_devs = [(dev,l) for dev,l in user_links_db.items() if l.get("status") == link_status_acceptable]
    if not candidate_devs:
        logging.warning(f"⚠️ No candidate links with status '{link_status_acceptable}' found for user {user_id} to handover, keeping current link {user_dev} if still available")
        return "", False
    for filter in handover_filters:
        if filter == "min_duration":
            # remove links with low remaining duration
            candidate_devs = filter_devs_min_duration(user_id, candidate_devs, metadata.get("min_remaining_duration_s", threshold_s), candidate_type="user")
        if filter == "min_orbit_hops":
            # remove links with orbit distance greather than the minimum ones among candidates
            candidate_devs = filter_devs_min_orbit_hops(user_id, candidate_devs, new_grd_dev, candidate_type="user", tolerance=min_hops_tolerance)
        if filter == "longest_duration":
            # remove links with remaining duration lower than the maximum one among candidates
            candidate_devs = filter_devs_longest_duration(user_id, candidate_devs, candidate_type="user")
        if filter == "min_delay":
            # remove links with delay higher than the minimum one among candidates
            candidate_devs = filter_devs_min_delay(user_id, candidate_devs, candidate_type="user")
    
    if not candidate_devs:
        logging.warning(f"⚠️ No candidate user links found for user {user_id} after filtering, keeping current user link {user_dev} if still available")
        return user_dev, False
    
    candidate_dev,_ = candidate_devs[0]

    if candidate_dev != user_dev:
        return candidate_dev,True
    else:
        return candidate_dev,False

def grd_handover_strategy(user_id: str, metadata: dict, new_user_dev: str, handover_filters: List[str]) -> Tuple[str, bool]:
    # Handover strategy for the grd link of the user SRv6 tunnel
    grd_dev = user_db.get(user_id, {}).get("grd_dev", None)
    if new_user_dev==None:
        new_user_dev = user_db.get(user_id, {}).get("user_dev", None)
    acceptable_link_status = "connected" # for grd dev we consider only connected links as candidates for handover
    grd_dev_last_duration = links_db.get(grd_dev, {}).get("last_duration", None)
    threshold_s = get_handover_threshold_s(metadata, grd_dev_last_duration)  # threshold for minimum remaining duration to enforce an handover
    load_balancing_tolerance = metadata.get("load_balancing_tolerance", 4)  # tolerance in n. connections for load balancing filter
    min_hops_tolerance = metadata.get("min_hops_tolerance", 0)  # tolerance in number of hops for min orbit hops filter
    grd_last_handover = user_db.get(user_id, {}).get("last_handover")
    if grd_last_handover is None:
        grd_last_handover = 0.0
    
    if grd_dev == None:
        # no grd link currently assigned to user, so handover is needed to assign a link
        remaining_duration = 0
    elif links_db.get(grd_dev,{}).get("status",None) != acceptable_link_status:
        # grd link is no more in an acceptable state, so handover is needed to assign a new link
        remaining_duration = 0
    else:
        remaining_duration = links_db.get(grd_dev, {}).get("last_duration", 0) - (time.time() - links_db.get(grd_dev, {}).get("last_created", 0))
    
    current_ok = (
        grd_dev is not None
        and links_db.get(grd_dev, {}).get("status") == acceptable_link_status
    )
    if current_ok and remaining_duration > threshold_s and time.time() - grd_last_handover < handover_hold_period_s:
        # no handover while current link is healthy and hold period is still active
        logging.debug(f"✅ Current grd link {grd_dev} for user {user_id} has remaining duration {remaining_duration:.1f}s and threshold {threshold_s:.1f}s, sec since last handover {time.time() - grd_last_handover:.1f}s and handover_hold_period_s {handover_hold_period_s:.1f}s, no handover needed")
        return grd_dev, False
    
    logging.debug(f"⏱️ Evaluating grd handover for user {user_id} on current grd link {grd_dev} with remaining duration {remaining_duration:.1f}s and threshold {threshold_s:.1f}s")
    # grd link has not enough remaining duration, handover needed
    candidate_devs = [(dev,l) for dev,l in links_db.items() if l.get("status") == acceptable_link_status]
    if not candidate_devs:
        return "", False
    for filter in handover_filters:
        if filter == "min_duration":
            # remove links with low remaining duration
            candidate_devs = filter_devs_min_duration(user_id, candidate_devs, metadata.get("min_remaining_duration_s", threshold_s), candidate_type="grd")
        if filter == "longest_duration":
            # remove links with remaining duration lower than the maximum one among candidates
            candidate_devs = filter_devs_longest_duration(user_id, candidate_devs, candidate_type="grd")
        if filter == "load_balancing":
            # remove links with user count greather than the minimum ones among candidates for load balancing
            candidate_devs = filter_devs_load_balancing(user_id, candidate_devs, load_balancing_tolerance, candidate_type="grd")
        if filter == "min_delay":
            # remove links with delay higher than the minimum one among candidates
            candidate_devs = filter_devs_min_delay(user_id, candidate_devs, candidate_type="grd")
        if user_id != node_name:
            # filters not applicable for grd default user access
            if filter == "min_orbit_hops":
                # remove links with orbit distance greather than the minimum ones among candidates
                candidate_devs = filter_devs_min_orbit_hops(user_id, candidate_devs, new_user_dev, candidate_type="grd",tolerance=min_hops_tolerance)
    if not candidate_devs:
        logging.warning(f"⚠️ No candidate grd links found for user {user_id} after filtering, keeping current grd link {grd_dev} if still available")
        return grd_dev, False
    
    candidate_dev,_ = candidate_devs[0]
 
    if candidate_dev != grd_dev:
        return candidate_dev,True
    else:
        return candidate_dev,False

def handover_strategy_coupled(user_id: str, metadata: dict, handover_filters: List[str]) -> Tuple[str, str, bool, bool]:

    threshold_s = metadata.get("threshold_s", handover_hold_period_s)
    last_handover = user_db.get(user_id, {}).get("last_handover")
    if last_handover is None:
        last_handover = 0.0
    # access_sat_couples
    user_link_db = user_db.get(user_id, {}).get("user_links_db", {}) or {}
    grd_link_db = links_db
    user_dev = user_db.get(user_id, {}).get("user_dev", None)
    grd_dev = user_db.get(user_id, {}).get("grd_dev", None)

    if user_dev == None:
        remaining_duration_usr_dev = 0
    elif user_link_db.get(user_dev,{}).get("status",None) != "available":
        remaining_duration_usr_dev = 0
    else:
        remaining_duration_usr_dev = user_link_db.get(user_dev, {}).get("last_duration", 0) - (time.time() - user_link_db.get(user_dev, {}).get("last_created", 0))
    if grd_dev == None:
        remaining_duration_grd_dev = 0
    elif grd_link_db.get(grd_dev,{}).get("status",None) != "connected":
        remaining_duration_grd_dev = 0
    else:
        remaining_duration_grd_dev = grd_link_db.get(grd_dev, {}).get("last_duration", 0) - (time.time() - grd_link_db.get(grd_dev, {}).get("last_created", 0))

    remaining_duration_couple = min(remaining_duration_usr_dev, remaining_duration_grd_dev)
    if remaining_duration_couple > threshold_s and (time.time() - last_handover) < handover_hold_period_s:
        logging.debug(f"✅ Current grd-user link couple ({grd_dev}, {user_dev}) for user {user_id} has remaining duration {remaining_duration_couple:.1f}s and threshold {threshold_s:.1f}s, sec since last handover {time.time() - last_handover:.1f}s and handover_hold_period_s {handover_hold_period_s:.1f}s, no handover needed")
        return grd_dev, user_dev, False, False
    
    # filtering phase
    candidate_grd_devs = [(dev, l) for dev, l in grd_link_db.items() if l.get("status") == "connected"]
    candidate_usr_devs = [(dev, link) for dev, link in user_link_db.items() if link.get("status") == "available"]
    access_sat_couples = [
        (candidate_grd_dev, candidate_usr_dev)
        for candidate_grd_dev, _ in candidate_grd_devs
        for candidate_usr_dev, _ in candidate_usr_devs
    ]
    for filter in handover_filters:
        if filter == "min_duration":
            access_sat_couples = filter_devs_min_duration_coupled(user_id, access_sat_couples, metadata.get("min_remaining_duration_s", threshold_s))
        if filter == "min_orbit_hops":
            access_sat_couples = filter_devs_min_orbit_hops_coupled(user_id, access_sat_couples, metadata.get("min_remaining_duration_s", threshold_s), tolerance=metadata.get("min_hops_tolerance", 0))
        if filter == "min_delay":
            access_sat_couples = filter_devs_min_delay_coupled(user_id, access_sat_couples, tolerance=metadata.get("min_delay_tolerance", 0))
        if filter == "longest_duration":
            access_sat_couples = filter_devs_longest_duration_coupled(user_id, access_sat_couples, metadata.get("longest_duration_tolerance", 0))
    if not access_sat_couples:
        logging.warning(f"⚠️ No candidate grd-user link couples found for user {user_id} after filtering, keeping current links ({grd_dev}, {user_dev}) if still available")
        return grd_dev, user_dev, False, False
    new_grd_dev, new_user_dev = access_sat_couples[0]
    if new_grd_dev == grd_dev and new_user_dev == user_dev:
        return grd_dev, user_dev, False, False
    elif new_grd_dev != grd_dev and new_user_dev != user_dev:
        return new_grd_dev, new_user_dev, True, True
    elif new_grd_dev != grd_dev and new_user_dev == user_dev:
        return new_grd_dev, user_dev, True, False
    elif new_grd_dev == grd_dev and new_user_dev != user_dev:
        return grd_dev, new_user_dev, False, True

def lifetime_strategy_connection_handover(metadata):
    # update of the connectted satellite links that can be used by users 
    max_dev = metadata.get("max_links", max_links)  # max number of simultaneous links (can be tuned based on expected number of available links and resource constraints)
    connected_links = [(dev,l) for dev,l in links_db.items() if l.get("status") == "connected"]
    unused_links = [
        (dev, link)
        for dev, link in connected_links
        if all(user_info.get("grd_dev") != dev for user_info in user_db.values())
    ]
    available_links = [(dev,l) for dev,l in links_db.items() if l.get("status") == "available"]
    used_links = len(connected_links) - len(unused_links)
    switchable_slots = max_dev - used_links
    if switchable_slots <= 0:
        logging.debug(f"✅ Maximum number of used links {max_dev} already reached with, no new connections can be established until some links become no more used by users")
        return

    # Keep the best switchable links among currently unused connected links and available links.
    switchable_links = unused_links + available_links
    sorted_switchable_links = sorted(
        switchable_links,
        key=lambda x: x[1].get("last_duration", 0) - (time.time() - x[1].get("last_created", 0)),
        reverse=True,
    )
    selected_switchable = sorted_switchable_links[:min(switchable_slots, len(sorted_switchable_links))]
    selected_switchable_devs = {dev for dev, _ in selected_switchable}

    for link_dev, _ in selected_switchable:
        if links_db.get(link_dev, {}).get("status") == "available":
            update_link_db(link_dev=link_dev, status="connected")
            sat_name = links_db.get(link_dev, {}).get("remote_endpoint_name", "unknown")
            logging.info(
                f"✅ Ground station connected with satellite {sat_name} with remaining duration "
                f"{(links_db.get(link_dev, {}).get('last_duration', 0) - (time.time() - links_db.get(link_dev, {}).get('last_created', 0))):.1f}s "
                "to fill available connection slots"
            )

    for link_dev, _ in unused_links:
        if link_dev not in selected_switchable_devs and links_db.get(link_dev, {}).get("status") == "connected":
            update_link_db(link_dev=link_dev, status="available")
            sat_name = links_db.get(link_dev, {}).get("remote_endpoint_name", "unknown")
            logging.info(
                f"⚠️ Ground station disconnected from satellite {sat_name} to free connection slots for better candidates with longer remaining duration"
            )


def processing_handover_loop() -> None:
    while True:
        try:
            logging.debug("🔄 Starting handover evaluation...")
            process_connection_handover(handover_metadata)  # evaluate if we need to switch some of the connected devs to new ones based on the connection handover strategy
            user_ids = list(user_db.keys())
            handover_shuffle_rng.shuffle(user_ids)
            for user_id in user_ids:
                if user_db[user_id].get("status") != USER_STATUS_REGISTERED:
                    logging.debug(f"⚠️ Skipping handover processing for user {user_id} which is not in registered state")
                    continue
                new_grd_dev, grd_needed = grd_handover_strategy(user_id, handover_metadata, None, grd_handover_filters)
                if user_id != node_name:  
                    # for grd pseudo-user we only have grd dev and no user dev, so we skip user handover evaluation for it as it's not applicable
                    new_user_dev, user_needed = user_handover_strategy(user_id, handover_metadata, new_grd_dev, user_handover_filters)
                else:
                    new_user_dev, user_needed = None, False  # self user doesn't have a satellite dev, so we skip user handover evaluation for it

                if grd_needed or user_needed:
                    strategy_type = "grd" if grd_needed and not user_needed else "user" if user_needed and not grd_needed else "grd+user"
                    # if new_user_dev:
                    #     new_user_sat_name = user_db.get(user_id, {}).get("user_links_db", {}).get(new_user_dev, {}).get("remote_endpoint_name", "unknown")
                    # else:
                    #     new_user_sat_name = "unknown"
                    # if grd_needed:
                    #     new_grd_sat_name = links_db.get(new_grd_dev, {}).get("remote_endpoint_name", "unknown")
                    if strategy_type == "grd+user":
                        new_user_sat_name = user_db.get(user_id, {}).get("user_links_db", {}).get(new_user_dev, {}).get("remote_endpoint_name", "unknown")
                        new_grd_sat_name = links_db.get(new_grd_dev, {}).get("remote_endpoint_name", "unknown")
                        logging.info(f"🔀 Handover type '{strategy_type}' for user {user_id}: selected newest grd satellite {new_grd_sat_name} and user satellite {new_user_sat_name}")
                    if strategy_type == "user":
                        new_user_sat_name = user_db.get(user_id, {}).get("user_links_db", {}).get(new_user_dev, {}).get("remote_endpoint_name", "unknown")
                        new_grd_sat_name = links_db.get(new_grd_dev, {}).get("remote_endpoint_name", "unknown")
                        logging.info(f"🔀 Handover type '{strategy_type}' for user {user_id}: selected newest user satellite {new_user_sat_name}")
                    elif strategy_type == "grd":
                        new_grd_sat_name = links_db.get(new_grd_dev, {}).get("remote_endpoint_name", "unknown")
                        if new_user_dev:
                            new_user_sat_name = user_db.get(user_id, {}).get("user_links_db", {}).get(new_user_dev, {}).get("remote_endpoint_name", "unknown")
                        else:                            
                            new_user_sat_name = "unknown"
                        logging.info(f"🔀 Handover type '{strategy_type}' for user {user_id}: selected newest grd satellite {new_grd_sat_name}")
                    
                    start_process_user_handover_thread(user_id = user_id, new_grd_dev = new_grd_dev, new_user_dev = new_user_dev)
                    
                    if report_file:
                        report_file.write(f"{time.time()},handover,{user_id},{strategy_type},{new_grd_sat_name},{new_user_sat_name}\n")
                        report_file.flush()
                    
        except Exception as e:
            logging.error("❌ Error in handover processing loop: %s", e)
        time.sleep(handover_periodic_check_s)  # periodic check interval for handover decision 

def processing_handover_loop_couple() -> None:
    while True:
        try:
            logging.debug("🔄 Starting handover evaluation...")
            process_connection_handover(handover_metadata)  # evaluate if we need to switch some of the connected devs to new ones based on the connection handover strategy
            user_ids = list(user_db.keys())
            handover_shuffle_rng.shuffle(user_ids)
            for user_id in user_ids:
                if user_db[user_id].get("status") != USER_STATUS_REGISTERED:
                    logging.debug(f"⚠️ Skipping handover processing for user {user_id} which is not in registered state")
                    continue
                if user_id == node_name:
                    new_grd_dev, grd_needed = grd_handover_strategy(user_id, handover_metadata, None, grd_handover_filters)
                    new_user_dev, user_needed = None, False  # self user doesn't have a satellite dev, so we skip user handover evaluation for it as it's not applicable
                else:
                    new_grd_dev, new_user_dev, grd_needed, user_needed = handover_strategy_coupled(user_id, handover_metadata, grd_handover_filters)
                if grd_needed or user_needed:
                    strategy_type = "grd" if grd_needed and not user_needed else "user" if user_needed and not grd_needed else "grd+user"
                    if strategy_type == "grd+user":
                        new_user_sat_name = user_db.get(user_id, {}).get("user_links_db", {}).get(new_user_dev, {}).get("remote_endpoint_name", "unknown")
                        new_grd_sat_name = links_db.get(new_grd_dev, {}).get("remote_endpoint_name", "unknown")
                        logging.info(f"🔀 Handover type '{strategy_type}' for user {user_id}: selected newest grd satellite {new_grd_sat_name} and user satellite {new_user_sat_name}")
                    if strategy_type == "user":
                        new_user_sat_name = user_db.get(user_id, {}).get("user_links_db", {}).get(new_user_dev, {}).get("remote_endpoint_name", "unknown")
                        new_grd_sat_name = links_db.get(new_grd_dev, {}).get("remote_endpoint_name", "unknown")
                        logging.info(f"🔀 Handover type '{strategy_type}' for user {user_id}: selected newest user satellite {new_user_sat_name}")
                    elif strategy_type == "grd":
                        new_grd_sat_name = links_db.get(new_grd_dev, {}).get("remote_endpoint_name", "unknown")
                        if new_user_dev:
                            new_user_sat_name = user_db.get(user_id, {}).get("user_links_db", {}).get(new_user_dev, {}).get("remote_endpoint_name", "unknown")
                        else:                            
                            new_user_sat_name = "unknown"
                        logging.info(f"🔀 Handover type '{strategy_type}' for user {user_id}: selected newest grd satellite {new_grd_sat_name}")
                    
                    start_process_user_handover_thread(user_id = user_id, new_grd_dev = new_grd_dev, new_user_dev = new_user_dev)
                    
                    if report_file:
                        report_file.write(f"{time.time()},handover,{user_id},{strategy_type},{new_grd_sat_name},{new_user_sat_name}\n")
                        report_file.flush()
                    
        except Exception as e:
            logging.error("❌ Error in handover processing loop: %s", e)
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
            if not is_user_active(user_id):
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
                    # remove the user route before marking the user unreachable
                    try:
                        ip_cmd = ["ip", "-6", "route", "del", user_ipv6, "dev", user_info.get("grd_dev", "unknown")]
                        run_cmd(ip_cmd)
                    except Exception as e:
                        logging.error("❌ Failed to remove route for user %s with IPv6 %s after missed heartbeats: %s", user_id, user_ipv6, e)
                # try:
                #     remove_qdisc_for_user(user_ipv6=user_info.get("user_ipv6", ""), user_id=user_id)
                # except Exception as e:
                #     logging.error("❌ Failed to remove qdisc state for user %s after missed heartbeats: %s", user_id, e)
                # user_db.pop(user_id, None)
                user_db[user_id]["status"] = "not-registered"  # mark user as not registered in the db after missed heartbeats, but keep its info for potential future handovers when it becomes reachable again
                with heartbeat_lock:
                    heartbeat_failures.pop(user_id, None)
                logging.warning("⚠️ Marked user %s as not-registered after %d missed heartbeats; keeping its state in user_db", user_id, misses)
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
    user = user_db.get(user_id, {})
    if not is_user_active(user_id):
        logging.warning(f"⚠️ Attempted local handover for user {user_id} which is not in registered state, skipping")
        return
    user_ipv6 = user.get("user_ipv6", "")
    
    # new values for handover
    grd_sat_ipv6 = links_db.get(new_grd_dev, {}).get("remote_endpoint_ipv6", "")
    user_sat_ipv6 = user.get("user_links_db", {}).get(new_user_dev, {}).get("remote_endpoint_ipv6", "") if new_user_dev else ""

    if not grd_sat_ipv6:
        logging.error(f"❌ No remote endpoint IPv6 found for new GRD dev {new_grd_dev} during handover of user {user_id}, aborting handover")
        if user_db.get(user_id, {}).get("status") == USER_STATUS_HANDOVER_IN_PROGRESS:
            update_user_db(user_id=user_id, status=USER_STATUS_REGISTERED)
        return
    if new_user_dev and not user_sat_ipv6:
        logging.error(f"❌ No remote endpoint IPv6 found for new USER dev {new_user_dev} during handover of user {user_id}, aborting handover")
        if user_db.get(user_id, {}).get("status") == USER_STATUS_HANDOVER_IN_PROGRESS:
            update_user_db(user_id=user_id, status=USER_STATUS_REGISTERED)
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
                            status=USER_STATUS_REGISTERED,
                            last_handover=time.time())
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
                           # if user_needed and handover_delay_ms > 0: 
        
        # apply traffic pause 
        if handover_delay_ms > 0:
            threading.Thread(
                    target=traffic_pause,
                    args=(user_id, handover_delay_ms),
                    daemon=True,
                    name=f"traffic-pause-{user_id}",
                ).start()

    except Exception as e:
        logging.error(f"❌ Local handover failed for user {user_id} : {e}")
        if user_db.get(user_id, {}).get("status") == USER_STATUS_HANDOVER_IN_PROGRESS:
            update_user_db(user_id=user_id, status=USER_STATUS_REGISTERED)
    return


def start_process_user_handover_thread(user_id: str, new_grd_dev=None, new_user_dev=None) -> None:
    user_status = user_db.get(user_id, {}).get("status")
    if user_status == USER_STATUS_HANDOVER_IN_PROGRESS:
        logging.debug("Skipping duplicate handover scheduling for user %s already in progress", user_id)
        return
    if user_status != USER_STATUS_REGISTERED:
        logging.debug("Skipping handover scheduling for user %s with status %s", user_id, user_status)
        return
    update_user_db(user_id=user_id, status=USER_STATUS_HANDOVER_IN_PROGRESS)
    threading.Thread(
        target=process_user_handover,
        args=(user_id, new_grd_dev, new_user_dev),
        daemon=True,
        name=f"user-handover-{user_id}",
    ).start()


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
        user_dev = payload.get("init_sat_dev", "")
        user_links_db = decode_user_links_db(payload.get("user_links_db", ""))
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
        update_user_db(user_id=user_id, user_ipv6=user_ipv6, user_links_db=user_links_db, user_dev=user_dev, status="registration_in_progress")
        
        # chose of the grd dev to serve the user    
        new_grd_dev, needed = grd_handover_strategy(user_id, handover_metadata, user_dev, grd_handover_filters)
        new_grd_sat_ipv6 = links_db.get(new_grd_dev, {}).get("remote_endpoint_ipv6", "") if new_grd_dev else ""
        
        if new_grd_dev == "" or new_grd_sat_ipv6 == "":
            logging.warning(f"⚠️ No suitable access satellite found for user {user_id}, registration aborted")
            # reset user state
            update_user_db(user_id=user_id, user_ipv6=user_ipv6, upstream_sids= None, downstream_sids=None, grd_dev=None, user_links_db=None, user_dev = None, status="not-registered")
            return
        
        # chose the access user dev
        new_user_dev, _ = user_handover_strategy(user_id, handover_metadata, new_grd_dev, user_handover_filters)
        new_user_sat_ipv6 = user_links_db.get(new_user_dev, {}).get("remote_endpoint_ipv6", "") if new_user_dev else ""
        if new_user_dev == "" or new_user_sat_ipv6 == "":
            logging.warning(f"⚠️ No suitable user satellite dev found for user {user_id}, registration aborted")
            # reset user state
            update_user_db(user_id=user_id, user_ipv6=user_ipv6, upstream_sids= None, downstream_sids=None, grd_dev=None, user_links_db=None, user_dev = None, status="not-registered")
            return
        new_grd_sat_name = links_db.get(new_grd_dev, {}).get("remote_endpoint_name", "unknown")
        new_user_sat_name = user_links_db.get(new_user_dev, {}).get("remote_endpoint_name", "unknown")
        logging.info(f"✅ Registration for user {user_id} accepted: grd satellite {new_grd_sat_name} and user satellite {new_user_sat_name}")

        if report_file:
            report_file.write(f"{time.time()},registration,{user_id},_,{new_grd_sat_name},{new_user_sat_name}\n")
            report_file.flush()
        # build sids for the SRv6 tunnels
        downstream_sids, upstream_sids = create_sids(new_grd_sat_ipv6, new_user_sat_ipv6)

        # user route injection
        ip_cmd = build_srv6_route_replace(dst_prefix = user_ipv6, sids = downstream_sids, dev = new_grd_dev)
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
            user_dev=new_user_dev,
            upstream_sids=upstream_sids,
            downstream_sids=downstream_sids,
            grd_dev=new_grd_dev,
            status="registered",
            last_handover=time.time()
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
        if not is_user_active(user_id):
            logging.debug("Ignoring measurement report for user %s not in registered state", user_id)
            return
        logging.debug(f"📊 Received measurement report from user {user_id}: {payload}")
        user_sat_ipv6 = payload.get("user_sat_ipv6", payload.get("current_sat_ipv6", ""))
        user_dev = payload.get("user_dev", payload.get("current_sat_dev", ""))
        user_links_db = decode_user_links_db(payload.get("user_links_db", ""))
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
    user_id = payload.get("user_id", "")
    if not is_user_active(user_id):
        logging.debug("Ignoring handover complete for user %s not in registered state", user_id)
        return
    logging.info(f"👤 Received handover complete from user {user_id}")
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
    user_ipv6 = payload.get("user_ipv6", "")

    ip_cmd = build_srv6_route_replace(dst_prefix=user_ipv6, sids=new_downstream_sids, dev=new_grd_dev, metric=20)
    run_cmd(ip_cmd)
    update_user_db(user_id=user_id, 
                    user_ipv6=user_ipv6, 
                    downstream_sids=new_downstream_sids, 
                    upstream_sids=payload.get("upstream_sids", ""),
                    grd_dev=new_grd_dev,
                    user_dev = new_user_dev,
                    status=USER_STATUS_REGISTERED,
                    last_handover=time.time())
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
                    threading.Thread(
                        target=handle_link_put_action,
                        args=(event,),
                        daemon=True,
                        name="link-put-handler",
                    ).start()
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
    global process_connection_handover, grd_ipv6, is_walker_star, user_callback_port, handover_metadata, link_duration_initial_value_s, link_setup_delay_s, max_links, handover_delay_ms, report_file, user_handover_filters, grd_handover_filters, handover_hold_period_s

    ap = argparse.ArgumentParser()
    ap.add_argument("--bind", default="::", help="Address to bind the UDP server for handover (default: :: for all interfaces)")
    ap.add_argument("--port", type=int, default=5005, help="UDP port where grd listens for handover_request (default: 5005)")
    ap.add_argument("--local-address", help="IPv6 address of local node (Default: address found in /etc/hosts for the hostname)")
    ap.add_argument("--user-handover-filters", type=str, default="min_duration,min_orbit_hops,longest_duration", help="comma separated value of filters to apply in sequence for user handover decisions (default: min_duration,min_orbit_hops,longest_duration)")
    ap.add_argument("--grd-handover-filters", type=str, default="min_duration,min_orbit_hops,load_balancing,longest_duration", help="comma separated value of filters to apply in sequence for grd handover decisions (default: min_duration,min_orbit_hops,load_balancing,longest_duration)")
    ap.add_argument("--handover-strategy-metadata", type=json.loads, default='{}', help="JSON string with metadata parameters for the handover strategy (e.g., threshold values, weights, etc.)")
    ap.add_argument("--handover-delay", type=float, help="Handover delay in mseconds (requires veth0_rt interface, use app/shaping-ns-create-v6.sh)", default=0)
    ap.add_argument("--usr-port", type=int, default=5006, help="Default UDP port where user agent listens for commands (default: 5006)")
    ap.add_argument("--sat-ipv6-prefix", default="2001:db8:100::/64", help="IPv6 prefix used for satellite IPv6 addresses (default: 2001:db8:100::/64)")
    ap.add_argument("--link-setup-delay", type=float, default=5, help="Estimated time in seconds needed by to setup relevat routes and interfaces after link creatio, default 5s)")
    ap.add_argument("--link-duration-initial-value", type=float, default=4*60, help="Initial value in seconds for the duration of new links, default: 4min)")
    ap.add_argument("--max-links", type=int, default=1024, help="Max number of simultaneous links (default: 1024)")
    ap.add_argument("--handover-hold-period", type=float, default=15.0, help="Hold period in seconds after a handover during which no new handover is allowed while the current link is healthy (default: 15s)")
    ap.add_argument("--walker-star", action="store_true", help="Set when the constellation is a Walker Star")
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
    is_walker_star = args.walker_star
    
    # Set handover filters preserving the exact comma-separated sequence provided by the user.
    accepted_user_filters = {"min_duration", "min_orbit_hops", "longest_duration","min_delay"}
    accepted_grd_filters = {"min_duration", "min_orbit_hops", "load_balancing", "longest_duration", "min_delay"}
    try:
        user_handover_filters = parse_handover_filters(
            args.user_handover_filters,
            accepted_user_filters,
            label="user",
        )
        grd_handover_filters = parse_handover_filters(
            args.grd_handover_filters,
            accepted_grd_filters,
            label="GRD",
        )
    except ValueError as e:
        logging.error("❌ %s", e)
        return
    logging.info(f"User handover decisions will be based on the following filters in sequence: {user_handover_filters}")
    logging.info(f"GRD handover decisions will be based on the following filters in sequence: {grd_handover_filters}")
    process_connection_handover = lifetime_strategy_connection_handover
    handover_metadata = args.handover_strategy_metadata
    handover_delay_ms = args.handover_delay
    handover_hold_period_s = args.handover_hold_period
    
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
        run_cmd(["/app/extra/QoS/shaping-ns-create-v6.sh"])

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
    
    preload_satellite_nodes_db_from_etcd(etcd_client)
    preload_links_db_from_etcd(etcd_client)
    
    # configure default route for satellites
    process_connection_handover(handover_metadata)
    new_grd_dev, needed = grd_handover_strategy(os.environ["NODE_NAME"], handover_metadata, None, grd_handover_filters)  # trigger initial handover decision for default route towards satellites based on initial links_db state (if any)
    if needed:
        new_sat_name = links_db.get(new_grd_dev, {}).get("remote_endpoint_name", "unknown")
        logging.info(f"🔀 Initial decision for {node_name} default route {args.sat_ipv6_prefix} via satellite {new_sat_name}")
        start_process_user_handover_thread(user_id=os.environ["NODE_NAME"], new_grd_dev=new_grd_dev)

    # Start background thread to watch for link actions and update links_db accordingly
    threading.Thread(
        target=watch_link_actions_loop,
        args=(etcd_client,),
        daemon=True,
        name="watch-link-actions",
        ).start()
    
    # Start background thread to periodically evaluate local handover decisions for users based on the selected strategy and current links_db state
    threading.Thread(
        target=processing_handover_loop_couple,
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
