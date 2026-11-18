#!/usr/bin/env python3
import argparse
import json
import logging
import os
import subprocess
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


links_db = {} # link info indexed by iface name (e.g., "vl_sat2_1": {endpoint1, endpoint2, delay, vni, etc.})
hosts_ipv6_cache: Dict[str, str] = {}
node_name = os.getenv("NODE_NAME")
KEY_LINKS_PREFIX = f"/config/links/{node_name}/"
link_duration_initial_value_s = 4*60  # initial value for link duration (sec)
dummy_dev_name = "sr0"
access_file = "/tmp/srv6_access.json"  # local file describing the current access satellite (see write_access_file)


# Status not_connected, connected, handover_in_progress
status = "not_connected" # initial status before initial connection
access_dev = None # current iface used for data transfer

# ho eligibility strategy function, set in main() based on args
chose_access_device = None
etcd_client = None
_UNSET = object()
access_check_lock = threading.RLock()  # serializes handle_access_check across threads
default_access_check_period_s = 10.0   # fallback period if handle_access_check returns no trigger time

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


def run_cmd(cmd: List[str]) -> None:
    res = subprocess.run(cmd, text=True, capture_output=True)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or f"Command failed: {' '.join(cmd)}")


def ensure_dev(name="sr0", fallback="eth0"):
    """Permanent placeholder interface for the learned routes."""
    
    if subprocess.run(["ip", "link", "show", name], capture_output=True, text=True).returncode != 0:
        r = subprocess.run(["ip", "link", "add", name, "type", "dummy"],
                           capture_output=True, text=True)
        if r.returncode != 0:                 # e.g. dummy module unavailable
            return fallback                   # eth0 is also permanent
    subprocess.run(["ip", "link", "set", name, "up"])
    subprocess.run(["ip", "link", "set", name, "mtu", "1400"])  # set MTU to 1400 for SRH overhead
    return name


def get_sat_orbit(sat_name: str):
    """Read the orbit number of a satellite from /config/nodes/<sat_name> (metadata.sat_config.sat_orbit)."""
    try:
        value, _ = etcd_client.get(f"/config/nodes/{sat_name}")
        if not value:
            return None
        node = json.loads(value.decode())
        return node.get("metadata", {}).get("sat_config", {}).get("sat_orbit")
    except Exception as e:
        logging.warning(f"⚠️ Could not read orbit of {sat_name} from Etcd: {e}")
        return None


def write_access_file(status: str, sat_name: str = "", sat_ipv6: str = "", orbit=None) -> None:
    """Atomically write the current access satellite (name, IPv6, orbit) to a local JSON file."""
    data = {"status": status, "satellite": sat_name, "ipv6": sat_ipv6, "orbit": orbit, "updated": time.time()}
    tmp = f"{access_file}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, access_file)
    except Exception as e:
        logging.warning(f"⚠️ Could not write access file {access_file}: {e}")


def build_srv6_default_route(access_sat_ipv6: str) -> List[str]:
    return [
        "ip", "-6", "route", "replace", "default","encap", "seg6", "mode", "inline", "segs", access_sat_ipv6.split("/")[0],
        "dev", dummy_dev_name
    ]

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


# ----------------------------
#   MAIN LOGIC
# ----------------------------


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


def handle_access_check() -> None:
    global status, access_dev
    with access_check_lock:  # re-entrant: safe to call from the watcher and the periodic thread
        return _handle_access_check_locked()

def _handle_access_check_locked():
    global status, access_dev
    access_dev, to_change, next_trigger_time_s = chose_access_device(handover_metadata) # chose of the initial dev to serve the user based on handover strategy 
    if not to_change:
        logging.debug(f"⏱️ No handover needed, current access device {access_dev} is still valid.")
        return next_trigger_time_s
    access_sat_name = links_db.get(access_dev, {}).get("remote_endpoint_name", "") if access_dev else ""
    if access_dev != "":
        access_sat_ipv6 = links_db.get(access_dev, {}).get("remote_endpoint_ipv6", "")
        if not access_sat_ipv6:
            logging.info(f"❌ Failed to resolve access satellite IPv6 address for satellite {access_sat_name}")
            return
        logging.info(f"🛰️ Chosen access satellite {access_sat_name}")
        # set default route via the chosen access satellite
        ip_cmd = build_srv6_default_route(access_sat_ipv6)
        run_cmd(ip_cmd)
        status = "connected"
        write_access_file("connected", access_sat_name, access_sat_ipv6.split("/")[0], get_sat_orbit(access_sat_name))
        logging.info(f"✅ Initial access completed via satellite {access_sat_name} on device {access_dev}")
    else:
        write_access_file("not_connected")
        logging.warning("⚠️ No available satellite links found for initial access, remaining in not_connected state.")
    return next_trigger_time_s
        
def lifetime_strategy(metadata: dict) -> Tuple[str, bool, float]:
    global access_dev, links_db, link_duration_initial_value_s
    # Example strategy: always prefer the link with greatest ttl
    lifetime_threshold_s = metadata.get("lifetime_threshold_s", link_duration_initial_value_s/4.0)  # threshold for minimum remaining duration to consider a handover
    # compute remaining duration for available links and select the one with the longest remaining duration above threshold
    if access_dev == None:
        # no link currently assigned to user, so handover is needed to assign the best available link
        current_remaining_duration = 0
    elif links_db.get(access_dev,{}).get("status",None) != "available":
        # current link is not available, so handover is needed to assign the best available link
        current_remaining_duration = 0
    else:
        current_remaining_duration = links_db.get(access_dev, {}).get("last_duration", 0) - (time.time() - links_db.get(access_dev, {}).get("last_created", 0))
    
    if current_remaining_duration > lifetime_threshold_s:
        # current link has enough remaining duration, no handover needed
        return access_dev, False, current_remaining_duration-lifetime_threshold_s
    
    available_devs = [(dev,l) for dev,l in links_db.items() if l.get("status") == "available"]
    if not available_devs:
        return "", False, 0.5  # no available links, return empty device and short retry period

    candidate_dev = max(available_devs, key=lambda x: x[1].get("last_duration", 0) - (time.time() - x[1].get("last_created", 0)))
    if candidate_dev[0] != access_dev:
        candidate_remaining_duration = candidate_dev[1].get("last_duration", 0) - (time.time() - candidate_dev[1].get("last_created", 0))
        return candidate_dev[0],True, max(candidate_remaining_duration-lifetime_threshold_s,0.5)
    else:
        return candidate_dev[0],False, max(current_remaining_duration-lifetime_threshold_s,0.5)  # no handover needed, return current access_dev and its remaining duration

# ----------------------------
#   WATCHERS
# ----------------------------
def watch_link_actions_loop () -> None:
    global status, access_dev
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
                    if status == "not_connected":
                        handle_access_check()

                elif isinstance(event, etcd3.events.DeleteEvent):
                    # update link_db
                    deleted_dev = event.key.decode().split("/")[-1]
                    remote_endpoint = links_db.get(deleted_dev, {}).get("remote_endpoint_name", "")
                    logging.info(f"➖ Detected out of range for satellite {remote_endpoint}")
                    last_duration = time.time() - links_db.get(deleted_dev, {}).get("last_created", time.time())
                    update_links_db(link_dev=deleted_dev, last_updated=time.time(), status="unavailable", last_duration=last_duration)
                    if deleted_dev == access_dev:
                        logging.warning(f"⚠️ Current access satellite {remote_endpoint} out of range, resetting state and re-connecting.")
                        status = "not_connected"
                        access_dev = None
                        write_access_file("not_connected")  # stays so if no other satellite is available
                        handle_access_check()
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


def access_check_loop() -> None:
    """Call handle_access_check periodically, using the period it returns (next_trigger_time_s)."""
    while True:
        period_s = default_access_check_period_s
        try:
            with access_check_lock:
                result = handle_access_check()
            if result is not None and result > 0:
                period_s = float(result)
        except Exception as ex:
            logging.error("❌ Access check failed (will retry in %.1fs): %s", period_s, ex)
        logging.debug("⏲️ Next access check in %.1fs", period_s)
        time.sleep(period_s)


# ----------------------------
#   ENTRYPOINT
# ----------------------------
def main() -> None:
    global chose_access_device, etcd_client, handover_metadata, link_duration_initial_value_s, dummy_dev_name, access_file

    ap = argparse.ArgumentParser()
    ap.add_argument("--handover-metadata", type=str, default="{}", help="JSON string with metadata for handover strategy")
    ap.add_argument("--handover-strategy", type=str, default="lifetime", choices=["lifetime"], help="Handover strategy to use (default: lifetime)")
    ap.add_argument("--link-duration-initial-value", type=float, default=4*60, help="Initial value in seconds for the duration of new links, default: 4min)")
    ap.add_argument("--dummy-dev-name", default="sr0", help="Name of the dummy device to use for default srv6 routes (default: sr0)")
    ap.add_argument("--access-file", default="/tmp/srv6_access.json", help="Local JSON file where the current access satellite (name, IPv6, orbit) is written (default: /tmp/srv6_access.json)")
    ap.add_argument("--log-level", default="INFO", help="Logging level (e.g., DEBUG, INFO, WARNING)")
    args = ap.parse_args()
    access_file = args.access_file

    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(message)s")
    
    etcd_client = get_etcd_client()

    refresh_hosts_ipv6_cache()
    
    dummy_dev_name = args.dummy_dev_name
    
    link_duration_initial_value_s = args.link_duration_initial_value
    handover_strategy = args.handover_strategy
    handover_metadata = json.loads(args.handover_metadata)

    if handover_strategy == "lifetime":
        chose_access_device = lifetime_strategy
    else:
        raise ValueError(f"Unknown handover strategy: {handover_strategy}")

    preload_links_db_from_etcd()
    ensure_dev(name=dummy_dev_name, fallback="eth0")

    threading.Thread(
        target=watch_link_actions_loop,
        daemon=True,
        name="watch-link-actions",
    ).start()

    # Non-daemon: first call runs immediately, then repeats every next_trigger_time_s
    # (also keeps the process alive after main() returns).
    threading.Thread(
        target=access_check_loop,
        daemon=False,
        name="access-check",
    ).start()

if __name__ == "__main__":
    main()
