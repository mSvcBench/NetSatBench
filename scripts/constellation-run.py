#!/usr/bin/env python3
import argparse
import json
import os
import sys
import time
import calendar
import glob
import re
import etcd3
import zlib
from typing import Optional, Tuple, List

# ==========================================
# GLOBALS
# ==========================================
TIME_OFFSET: Optional[float] = None


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


def smart_wait(target_virtual_time_str, filename: str, enable_wait: bool = True) -> None:
    """
    Syncs the emulation virtual time with real wall-clock time.
    If enable_wait is False, it will not sleep.
    """
    global TIME_OFFSET

    if not enable_wait:
        return

    if not target_virtual_time_str:
        return

    try:
        virtual_time = float(target_virtual_time_str)
        current_real_time = time.time()

        # Initialize baseline on the first epoch
        if TIME_OFFSET is None:
            TIME_OFFSET = current_real_time - virtual_time
            print(f"⏱️  [{filename}] Baseline set. Virtual Epoch Time: {virtual_time}")
            return

        target_real_time = virtual_time + TIME_OFFSET
        delay = target_real_time - time.time()

        if delay > 0:
            #print(f"⏳ [{filename}] Waiting {delay:.1f}s to sync epoch...")
            time.sleep(delay)

    except ValueError:
        print(f"⚠️ [{filename}] Invalid time format: {target_virtual_time_str}")


def connect_etcd(etcd_host: str, etcd_port: int):
    try:
        print(f"📁 Connecting to Etcd at {etcd_host}:{etcd_port}...")
        client = etcd3.client(host=etcd_host, port=etcd_port)
        client.status()  # quick sanity check
        return client
    except Exception as e:
        print(f"❌ Could not connect to Etcd at {etcd_host}:{etcd_port}. Is it running?")
        print(f"Details: {e}")
        sys.exit(1)


def load_epoch_dir_and_pattern_from_etcd(etcd) -> Tuple[str, str]:
    """
    Reads /config/epoch-config from Etcd if present, otherwise returns defaults.
    """
    default_dir = "constellation-epochs"
    default_pattern = "NetSatBench-epoch*.json"

    try:
        epoch_config_value, _ = etcd.get("/config/epoch-config")
        if not epoch_config_value:
            return default_dir, default_pattern

        epoch_config = json.loads(epoch_config_value.decode("utf-8"))
        epoch_dir = epoch_config.get("epoch-dir", default_dir)
        file_pattern = epoch_config.get("file-pattern", default_pattern)
        return epoch_dir, file_pattern

    except Exception as e:
        print(f"⚠️ Failed to load epoch configuration from Etcd, using defaults. Details: {e}")
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

# ==========================================
# 🚀 CORE LOGIC
# ==========================================
def apply_single_epoch(json_path: str, etcd, enable_wait: bool = True) -> None:
    filename = os.path.basename(json_path)

    try:
        with open(json_path, "r", encoding="utf-8") as f:
            config = json.load(f)

        # WAIT FOR SCHEDULED TIME (virtual -> real time sync)
        epoch_time = config.get("epoch-time") if config.get("epoch-time") else convert_time_epoch_to_timestamp(config.get("time"))
        smart_wait(epoch_time, filename, enable_wait=enable_wait)
        print(f"🚩 [{filename}] Applying epoch configuration at virtual epoch time {epoch_time}...")
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

        # A. Push satellites/users/grounds + epoch-time
        for key, value in config.items():
            if key not in allowed_keys:
                print(f"❌ [{filename}] Unexpected key '{key}' found in epoch file, skipping...")
                continue

            if key == "epoch-time":
                etcd.put("/config/epoch-time", str(value).strip().replace('"', ''))

            elif key in ["satellites", "users", "grounds", "run"]:
                # NOTE: this keeps your original behavior (even though "run" is also handled later).
                for k, v in value.items():
                    etcd.put(f"/config/{key}/{k}", json.dumps(v))

        # B. Push Dynamic Actions
        add = config.get("links-add", [])
        delete = config.get("links-del", [])          # fixed key name vs your current "link-delete"
        update = config.get("links-update", [])       # fixed key name vs your current "link-update"

        for l in add:
            ep2_antenna = l.get("endpoint2_antenna", 1)
            ep1_antenna = l.get("endpoint1_antenna", 1)
            vxlan_iface_name1 = f"vl_{l['endpoint2']}_{ep2_antenna}"
            vxlan_iface_name2 = f"vl_{l['endpoint1']}_{ep1_antenna}"
            etcd_key1 = f"/config/links/{l['endpoint1']}_/{vxlan_iface_name1}"
            etcd_key2 = f"/config/links/{l['endpoint2']}_/{vxlan_iface_name2}"

            vni = calculate_vni(
                l["endpoint1"], ep1_antenna,
                l["endpoint2"], ep2_antenna,
            )
            l["vni"] = vni
            print(f"🪢  [{filename}] Syncing link-add {l['endpoint1']} - {l['endpoint2']} with VNI {vni}")

            etcd.put(etcd_key1, json.dumps(l))
            etcd.put(etcd_key2, json.dumps(l))

        for l in delete:
            ep2_antenna = l.get("endpoint2_antenna", 1)
            ep1_antenna = l.get("endpoint1_antenna", 1)
            vxlan_iface_name1 = f"vl_{l['endpoint2']}_{ep2_antenna}"
            vxlan_iface_name2 = f"vl_{l['endpoint1']}_{ep1_antenna}"
            etcd_key1 = f"/config/links/{l['endpoint1']}_/{vxlan_iface_name1}"
            etcd_key2 = f"/config/links/{l['endpoint2']}_/{vxlan_iface_name2}"

            vni = calculate_vni(
                l["endpoint1"], ep1_antenna,
                l["endpoint2"], ep2_antenna,
            )
            print(f"✂️  [{filename}] Syncing link-del {l['endpoint1']} - {l['endpoint2']} (VNI {vni})")

            etcd.delete(etcd_key1)
            etcd.delete(etcd_key2)

        for l in update:
            ep2_antenna = l.get("endpoint2_antenna", 1)
            ep1_antenna = l.get("endpoint1_antenna", 1)
            vxlan_iface_name1 = f"vl_{l['endpoint2']}_{ep2_antenna}"
            vxlan_iface_name2 = f"vl_{l['endpoint1']}_{ep1_antenna}"
            etcd_key1 = f"/config/links/{l['endpoint1']}_/{vxlan_iface_name1}"
            etcd_key2 = f"/config/links/{l['endpoint2']}_/{vxlan_iface_name2}"

            # Sanity check: ensure the link exists before updating
            etcd_value1, _ = etcd.get(etcd_key1)
            etcd_value2, _ = etcd.get(etcd_key2)
            if not etcd_value1 or not etcd_value2:
                print(f"⚠️  [{filename}] Link not found in Etcd for {l['endpoint1']} - {l['endpoint2']}. Skipping update.")
                continue

            vni = calculate_vni(
                l["endpoint1"], ep1_antenna,
                l["endpoint2"], ep2_antenna,
            )
            l["vni"] = vni

            print(f"♻️  [{filename}] Syncing link-update {l['endpoint1']} - {l['endpoint2']} (VNI {vni})")
            etcd.put(etcd_key1, json.dumps(l))
            etcd.put(etcd_key2, json.dumps(l))

        # D. Push Runtime Commands
        for node, cmds in config.get("run", {}).items():
            etcd.put(f"/config/run/{node}_", json.dumps(cmds))

        print(f"✅ [{filename}] Epoch applied successfully.")

    except Exception as e:
        print(f"❌ Error processing {filename}: {e}")


def run_all_epochs(
    etcd,
    epoch_dir: Optional[str],
    file_pattern: Optional[str],
    enable_wait: bool = True,
) -> int:
    if epoch_dir is None or file_pattern is None:
        epoch_dir_etcd, file_pattern_etcd = load_epoch_dir_and_pattern_from_etcd(etcd)
        epoch_dir = epoch_dir or epoch_dir_etcd
        file_pattern = file_pattern or file_pattern_etcd

    files = list_epoch_files(epoch_dir, file_pattern)
    if not files:
        print(f"⚠️ No epoch files found in {os.path.join(epoch_dir, file_pattern)}")
        return 1

    print(f"🚀 Starting emulation with {len(files)} epochs found.")
    for f in files:
        apply_single_epoch(f, etcd, enable_wait=enable_wait)

    return 0


# ==========================================
# 🏁 MAIN
# ==========================================
def main() -> int:
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

    # “config file at least”: here it is the epoch-config JSON, but we also allow overriding dir/pattern directly.
    parser.add_argument(
        "-c", "--epoch-config",
        help="Optional path to an epoch-config JSON file (same structure as /config/epoch-config). "
             "If provided, it overrides the Etcd /config/epoch-config values.",
    )
    parser.add_argument(
        "--epoch-dir",
        help="Override epoch directory (takes precedence over Etcd and --epoch-config).",
    )
    parser.add_argument(
        "--file-pattern",
        help="Override epoch filename pattern (takes precedence over Etcd and --epoch-config).",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Disable virtual-time synchronization (do not sleep on epoch-time).",
    )

    args = parser.parse_args()

    etcd = etcd3.client(host=args.etcd_host, port=args.etcd_port)
    try:
        etcd.status()
    except Exception as e:
        print(f"❌ Could not connect to Etcd at {args.etcd_host}:{args.etcd_port}. Is it running?")
        print(f"Details: {e}")
        return 2

    # If an epoch-config file is provided, load it and use it unless user overrides epoch-dir/pattern explicitly.
    epoch_dir = args.epoch_dir
    file_pattern = args.file_pattern
    if args.epoch_config:
        try:
            with open(args.epoch_config, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            epoch_dir = epoch_dir or cfg.get("epoch-dir")
            file_pattern = file_pattern or cfg.get("file-pattern")
        except FileNotFoundError:
            print(f"❌ Error: epoch-config file '{args.epoch_config}' not found.")
            return 2
        except json.JSONDecodeError as e:
            print(f"❌ Error: failed to parse epoch-config JSON '{args.epoch_config}': {e}")
            return 2

    enable_wait = not args.no_wait
    return run_all_epochs(etcd, epoch_dir, file_pattern, enable_wait=enable_wait)


if __name__ == "__main__":
    raise SystemExit(main())
