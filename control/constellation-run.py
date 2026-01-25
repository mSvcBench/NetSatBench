#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import sys
import threading
import time
import calendar
import glob
import re
import etcd3
import zlib
import shutil
import logging
from typing import Optional, Tuple, List
from pathlib import Path
import os
import pyinotify
from pathlib import Path


logging.basicConfig(level="INFO", format="[%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ==========================================
# GLOBALS
# ==========================================
TIME_OFFSET: Optional[float] = None
etcd_client = None
writing_lock = threading.Lock()


# ==========================================
# 🧮 HELPERS
# ==========================================
def calculate_vni(ep1, ant1, ep2, ant2) -> int:
    """
    Generates a deterministic VNI based on endpoints and antennas.
    """
    # Sort endpoints to ensure A->B and B->A produce the same VNI
    if str(ep1) < str(ep2):
        val = f"{ep1}_{ant1}_{ep2}_{ant2}"
    else:
        val = f"{ep2}_{ant2}_{ep1}_{ant1}"

    checksum = zlib.crc32(val.encode("utf-8"))
    return (checksum % 16777215) + 1


def smart_wait(target_virtual_time_str, filename: str, fixed_wait: int = -1) -> None:
    global TIME_OFFSET
    
    if fixed_wait != -1:
        time.sleep(fixed_wait)  
        return

    if not target_virtual_time_str:
        return

    try:
        virtual_time = float(target_virtual_time_str)

        # Initialize baseline on the first epoch
        if TIME_OFFSET is None:
            TIME_OFFSET = virtual_time
            log.debug(f"⏱️  [{filename}] Baseline set. Virtual Epoch Time: {virtual_time}")
            return

        delay = virtual_time - TIME_OFFSET
        TIME_OFFSET = virtual_time

        if delay > 0:
            time.sleep(delay)
            log.debug(f"⏳ [{filename}] Waiting {delay:.1f}s to sync epoch...")

    except ValueError:
        log.warning(f"⚠️ [{filename}] Invalid time format: {target_virtual_time_str}")


def connect_etcd(etcd_host: str, etcd_port: int, etcd_user = None, etcd_password = None, etcd_ca_cert = None):
    try:
        log.info(f"📁 Connecting to Etcd at {etcd_host}:{etcd_port}...")
        if etcd_user and etcd_password:
            return etcd3.client(host=etcd_host, port=etcd_port, user=etcd_user, password=etcd_password, ca_cert=etcd_ca_cert)
        else:
            return etcd3.client(host=etcd_host, port=etcd_port)
    except Exception as e:
        log.error(f"❌ Failed to initialize Etcd client: {e}")
        sys.exit(1)

def load_epoch_dir_and_pattern_from_etcd() -> Tuple[str, str]:
    """
    Reads /config/epoch-config from Etcd if present, otherwise returns defaults.
    """
    default_dir = "constellation-epochs"
    default_pattern = "NetSatBench-epoch*.json"

    try:
        epoch_config_value, _ = etcd_client.get("/config/epoch-config")
        if not epoch_config_value:
            return default_dir, default_pattern

        epoch_config = json.loads(epoch_config_value.decode("utf-8"))
        epoch_dir = epoch_config.get("epoch-dir", default_dir)
        file_pattern = epoch_config.get("file-pattern", default_pattern)
        return epoch_dir, file_pattern

    except Exception as e:
        log.warning(f"⚠️ Failed to load epoch configuration from Etcd, using defaults. Details: {e}")
        return default_dir, default_pattern


def list_epoch_files(epoch_dir: str, file_pattern: str) -> List[str]:
    if not epoch_dir or not file_pattern:
        return []

    search_path = os.path.join(epoch_dir, file_pattern)

    def last_numeric_suffix(path: str) -> int:
        """
        Extracts the last contiguous sequence of digits from the filename
        and returns it as an integer.
        If no digits are found, returns -1.
        """
        basename = os.path.basename(path)
        matches = re.findall(r"(\d+)", basename)
        return int(matches[-1]) if matches else -1

    files = sorted(glob.glob(search_path), key=last_numeric_suffix)
    return files

def convert_time_epoch_to_timestamp(time_str: str) -> float:
    """
    Converts an ISO-8601 UTC time string
    'YYYY-MM-DDTHH:MM:SSZ'
    to a Unix timestamp (seconds since epoch).
    """
    try:
        struct_time = time.strptime(time_str, "%Y-%m-%dT%H:%M:%SZ")
        return calendar.timegm(struct_time)  # UTC-safe
    except ValueError:
        raise ValueError(
            f"❌ Invalid time format: {time_str}. "
            "Expected 'YYYY-MM-DDTHH:MM:SSZ'."
        )



class NewEpochFileHandler(pyinotify.ProcessEvent):
    def process_IN_CLOSE_WRITE(self, event):
        # Ignore directories
        if event.dir:
            return
        path = event.pathname
        process_epoch_from_queue(path)
        # delete the file after processing
        os.remove(path)


def start_queue_watcher(path: Path):
    wm = pyinotify.WatchManager()
    handler = NewEpochFileHandler()
    notifier = pyinotify.ThreadedNotifier(wm, handler)
    notifier.daemon = True
    notifier.start()
    mask = pyinotify.IN_CLOSE_WRITE
    wm.add_watch(str(path), mask, rec=False)
    log.info(f"👀 Watching epoch queue (IN_CLOSE_WRITE): {path}")
    return notifier


# ==========================================
# 🚀 CORE LOGIC
# ==========================================
def process_epoch_from_queue(json_path: str) -> None:
    with open(json_path, "r", encoding="utf-8") as f:
            epoch_dict = json.load(f)
    # Allowed keys in epoch file
    allowed_keys = [
        "epoch-time",
        "links-add",
        "links-del",
        "links-update",
        "run",
        "satellites",
        "users",
        "grounds",
        "time",
    ]
    # A. Push epoch-time and sanity check
    for key, value in epoch_dict.items():
        if key not in allowed_keys:
            log.warning(f"❌ [{os.path.basename(json_path)}] Unexpected key '{key}' found in epoch file, skipping...")
            continue
        if key == "epoch-time":
            etcd_client.put("/config/epoch-time", str(value).strip().replace('"', ''))

    # B. Push Dynamic Actions
    add = epoch_dict.get("links-add", [])
    delete = epoch_dict.get("links-del", [])          # fixed key name vs your current "link-delete"
    update = epoch_dict.get("links-update", [])       # fixed key name vs your current "link-update"

    for l in add:
        ep2_antenna = l.get("endpoint2_antenna", 1)
        ep1_antenna = l.get("endpoint1_antenna", 1)
        vxlan_iface_name1 = f"vl_{l['endpoint2']}_{ep2_antenna}"
        vxlan_iface_name2 = f"vl_{l['endpoint1']}_{ep1_antenna}"
        etcd_key1 = f"/config/links/{l['endpoint1']}/{vxlan_iface_name1}"
        etcd_key2 = f"/config/links/{l['endpoint2']}/{vxlan_iface_name2}"

        vni = calculate_vni(
            l["endpoint1"], ep1_antenna,
            l["endpoint2"], ep2_antenna,
        )
        l["vni"] = vni
        log.debug(f"🛜  [{os.path.basename(json_path)}] Syncing link-add {l['endpoint1']} - {l['endpoint2']} with VNI {vni}")

        etcd_client.put(etcd_key1, json.dumps(l))
        etcd_client.put(etcd_key2, json.dumps(l))

    for l in delete:
        ep2_antenna = l.get("endpoint2_antenna", 1)
        ep1_antenna = l.get("endpoint1_antenna", 1)
        vxlan_iface_name1 = f"vl_{l['endpoint2']}_{ep2_antenna}"
        vxlan_iface_name2 = f"vl_{l['endpoint1']}_{ep1_antenna}"
        etcd_key1 = f"/config/links/{l['endpoint1']}/{vxlan_iface_name1}"
        etcd_key2 = f"/config/links/{l['endpoint2']}/{vxlan_iface_name2}"

        vni = calculate_vni(
            l["endpoint1"], ep1_antenna,
            l["endpoint2"], ep2_antenna,
        )
        log.debug(f"✂️  [{os.path.basename(json_path)}] Syncing link-del {l['endpoint1']} - {l['endpoint2']} (VNI {vni})")

        etcd_client.delete(etcd_key1)
        etcd_client.delete(etcd_key2)

    for l in update:
        ep2_antenna = l.get("endpoint2_antenna", 1)
        ep1_antenna = l.get("endpoint1_antenna", 1)
        vxlan_iface_name1 = f"vl_{l['endpoint2']}_{ep2_antenna}"
        vxlan_iface_name2 = f"vl_{l['endpoint1']}_{ep1_antenna}"
        etcd_key1 = f"/config/links/{l['endpoint1']}/{vxlan_iface_name1}"
        etcd_key2 = f"/config/links/{l['endpoint2']}/{vxlan_iface_name2}"

        # Sanity check: ensure the link exists before updating
        etcd_value1, _ = etcd_client.get(etcd_key1)
        etcd_value2, _ = etcd_client.get(etcd_key2)
        if not etcd_value1 or not etcd_value2:
            log.warning(f"⚠️  [{os.path.basename(json_path)}] Link not found in Etcd for {l['endpoint1']} - {l['endpoint2']}. Skipping update.")
            continue

        vni = calculate_vni(
            l["endpoint1"], ep1_antenna,
            l["endpoint2"], ep2_antenna,
        )
        l["vni"] = vni

        log.debug(f"♻️  [{os.path.basename(json_path)}] Syncing link-update {l['endpoint1']} - {l['endpoint2']} (VNI {vni})")
        etcd_client.put(etcd_key1, json.dumps(l))
        etcd_client.put(etcd_key2, json.dumps(l))

    # D. Push Runtime Commands
    for node, cmds in epoch_dict.get("run", {}).items():
        log.debug(f"▶️  [{os.path.basename(json_path)}] Pushing run commands to node {node}: {cmds}")
        etcd_client.put(f"/config/run/{node}", json.dumps(cmds))

    log.info(f" ✅ [{os.path.basename(json_path)}] Epoch applied successfully.")

def process_epoch_from_dir(json_path: str, queue_path: str, fixed_wait: int = -1) -> None:
    filename = os.path.basename(json_path)
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            config = json.load(f)

        # WAIT FOR SCHEDULED TIME (virtual -> real time sync)
        epoch_time = config.get("epoch-time") if config.get("epoch-time") else convert_time_epoch_to_timestamp(config.get("time"))
        smart_wait(epoch_time, filename, fixed_wait=fixed_wait)
        log.info(f"🚩 Applying epoch configuration {filename}...")
        with writing_lock:
            # copy epoch file in the queque directory
            shutil.copy2(json_path, os.path.join(queue_path, filename))

    except Exception as e:
        log.error(f"❌ Error processing {filename}: {e}")


def run_all_epochs(
    epoch_dir: str,
    file_pattern: str,
    queue_path: str,
    fixed_wait: bool = True,
    loop_delay: Optional[int] = None,
) -> int:

    global TIME_OFFSET
    files = list_epoch_files(epoch_dir, file_pattern)
    if not files:
        log.warning(f"⚠️ No epoch files found in {os.path.join(epoch_dir, file_pattern)}")
        return 1

    log.info(f"🚀 Starting emulation with {len(files)} epochs found.")
    while True:
        for f in files:
            process_epoch_from_dir(json_path=f, queue_path=queue_path, fixed_wait=fixed_wait)
        if loop_delay is not None:
            log.info(f"🔄 Looping emulation after {loop_delay} seconds...")
            time.sleep(loop_delay)
            TIME_OFFSET = None  # reset time offset for next loop
        else:
            time.sleep(30)  # brief pause before exiting to allow final epoch processing
            break
    return 0


async def watch_epoch_queue(path: Path):
    loop = asyncio.get_running_loop()
    handler = NewEpochFileHandler()
    observer = Observer()
    observer.schedule(handler, str(path), recursive=False)
    observer.start()
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        observer.stop()
        observer.join()
    
# ==========================================
# 🏁 MAIN
# ==========================================
def main() -> int:
    global etcd_client

    parser = argparse.ArgumentParser(
        description="Apply all epoch JSON files to Etcd (with optional virtual-time synchronization)."
    )
    parser.add_argument(
        "--etcd-host",
        default=os.getenv("ETCD_HOST", "127.0.0.1"),
        help="Etcd host (default: env ETCD_HOST or 127.0.0.1)",
    )
    parser.add_argument(
        "--etcd-port",
        type=int,
        default=int(os.getenv("ETCD_PORT", 2379)),
        help="Etcd port (default: env ETCD_PORT or 2379)",
    )
    parser.add_argument(
        "--etcd-user",
        default=os.getenv("ETCD_USER", None ),
        help="Etcd user (default: env ETCD_USER or None)",
    )
    parser.add_argument(
        "--etcd-password",
        default=os.getenv("ETCD_PASSWORD", None ),
        help="Etcd password (default: env ETCD_PASSWORD or None)",
    )
    parser.add_argument(
        "--epoch-dir",
        help="Override epoch directory (takes precedence over Etcd).",
    )
    parser.add_argument(
        "--file-pattern",
        help="Override epoch filename pattern (takes precedence over Etcd).",
    )
    parser.add_argument(
        "--fixed-wait",
        default=-1,
        type=int,
        help="Disable virtual-time synchronization (do constant sleep on epoch-time).",
    )
    parser.add_argument(
        "--loop-delay",
        default=None,
        type=int,
        help="Enable loop repeat with a fixed delay between last and first epochs (in seconds)."
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Enable interactive mode to watch epoch-queue folder for new epochs. No epoch directory scanning.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (default: INFO)",
    )

    args = parser.parse_args()
    log.setLevel(args.log_level.upper())

    etcd_client = etcd3.client(host=args.etcd_host, port=args.etcd_port)
    try:
        etcd_client.status()
    except Exception as e:
        log.error(f"❌ Could not connect to Etcd at {args.etcd_host}:{args.etcd_port}. Is it running?")
        log.error(f"Details: {e}")
        return 2

    # If an epoch-config file is provided, load it and use it unless user overrides epoch-dir/pattern explicitly.
    epoch_dir = args.epoch_dir
    file_pattern = args.file_pattern
    if epoch_dir is None or file_pattern is None:
        epoch_dir_etcd, file_pattern_etcd = load_epoch_dir_and_pattern_from_etcd()
        epoch_dir = epoch_dir or epoch_dir_etcd
        file_pattern = file_pattern or file_pattern_etcd
    epoch_queue_dir = os.path.join(epoch_dir, "epoch-queue")
    # Ensure the queue directory exists
    os.makedirs(epoch_queue_dir, exist_ok=True)
    
    # Start watching the queue directory in a separate thread
    queue_observer = start_queue_watcher(Path(epoch_queue_dir))

    fixed_wait = args.fixed_wait
    # Start Event Loops
    try:
        if args.interactive:
            log.info("🖥️  Running in interactive mode. Watching epoch-queue for new epochs...")
            while True:
                time.sleep(3600)
        else:                           
            return run_all_epochs(epoch_dir=epoch_dir, 
                              file_pattern=file_pattern, 
                              queue_path=epoch_queue_dir, 
                              fixed_wait=fixed_wait, 
                              loop_delay=args.loop_delay)
    finally:
        queue_observer.stop()
        queue_observer.join()


if __name__ == "__main__":
    raise SystemExit(main())
