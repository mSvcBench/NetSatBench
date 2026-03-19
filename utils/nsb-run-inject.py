
#!/usr/bin/env python3
# ==========================================
# MAIN
# ==========================================
import csv
from glob import glob
import json
import os
import re
import numpy as np
import argparse
import logging
from typing import List, Dict, Optional, Tuple, Any
from datetime import datetime, timedelta, timezone
from collections import deque, defaultdict

logging.basicConfig(level="INFO", format="[%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

#--------------------------------
# HELPER FUNCTIONS
#--------------------------------
def list_epoch_files(epoch_dir: str, file_pattern: str) -> List[str]:
    if not epoch_dir or not file_pattern:
        return []

    search_path = os.path.join(epoch_dir, file_pattern)

    def last_numeric_suffix(path: str) -> int:
        basename = os.path.basename(path)
        matches = re.findall(r"(\d+)", basename)
        return int(matches[-1]) if matches else -1

    return sorted(glob(search_path), key=last_numeric_suffix)

def find_epoch_file_for_time(epoch_files: List[str], target_time: datetime) -> Optional[str]:
    for epoch_file in epoch_files:
        with open(epoch_file, "r") as f:
            epoch_data = json.load(f)
        epoch_time_str = epoch_data.get("time")
        if not epoch_time_str:
            continue
        try:
            epoch_time = datetime.fromisoformat(epoch_time_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        if epoch_time >= target_time:
            return epoch_file
    return None


def parse_command_list(command_list: str) -> List[str]:
    try:
        parsed_rows = list(csv.reader([command_list], skipinitialspace=True))
    except csv.Error as exc:
        raise ValueError(f"Invalid command list: {exc}") from exc
    if not parsed_rows:
        return []
    return [cmd.strip() for cmd in parsed_rows[0] if cmd.strip()]

#--------------------------------
# MAIN FUNCTION
#--------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Inject a run command into the epoch file after a given offset time for a given node or node type.")

    parser.add_argument(
        "-c", "--config",
        default="sat-config.json",
        help="Path to the JSON sat configuration file (e.g., sat-config.json)",
    )
    parser.add_argument("--offset-seconds", type=int, default=-1, help="Offset in seconds from the target time to inject the command")
    parser.add_argument("--target-time", type=str, default="", help="Target time in ISO format (e.g., 2024-01-01T12:00:00Z)")
    parser.add_argument("--node", type=str,default="", help="Node name to target (e.g., node1)")
    parser.add_argument("--node-type-list", type=str, default="", help="Comma-separated list of node types, one per command in --command-list")
    parser.add_argument("--command-list", type=str, required=True, help="Comma-separated list of commands to inject (e.g., 'echo Hello World')")
    parser.add_argument("--log-level", type=str, default="INFO", help="Logging level (e.g., DEBUG, INFO, WARNING, ERROR)")
    
    args = parser.parse_args()
    log.setLevel(args.log_level.upper())

    # Load configuration
    with open(args.config, "r") as f:
        config = json.load(f)
    epoch_dir = config.get("epoch-config", {}).get("epoch-dir", {})
    file_pattern = config.get("epoch-config", {}).get("file-pattern", {})
    
    if not epoch_dir or not file_pattern:
        log.error("Epoch directory or file pattern not specified in configuration.")
        return 1
    
    epoch_files = list_epoch_files(epoch_dir, file_pattern)
    if not epoch_files:
        log.error("No epoch files found in the specified directory with the given pattern.")
        return 1
    
    # ensure node or node type list is specified
    if not args.node and not args.node_type_list:
        log.error("Either node or node type must be specified.")
        return 1
    if args.node and args.node_type_list:
        log.error("Cannot specify both node and node type list. Please choose one.")
        return 1
    
    # ensure target time or offset seconds are specified
    if not args.target_time and args.offset_seconds < 0:
        log.error("Either target time or offset seconds must be specified.")
        return 1
    
    # ensure target time or offset seconds are not both specified
    if args.target_time and args.offset_seconds >= 0:
        log.error("Cannot specify both target time and offset seconds. Please choose one.")
        return 1
    if args.target_time:
        try:
            target_time = datetime.fromisoformat(args.target_time.replace("Z", "+00:00"))
        except ValueError:
            log.error("Invalid target time format. Use ISO format (e.g., 2024-01-01T12:00:00Z).")
            return 1
        if target_time.tzinfo is None:
            target_time = target_time.replace(tzinfo=timezone.utc)
    else:
        if args.offset_seconds < 0:
            log.error("Offset seconds must be non-negative when target time is not specified.")
            return 1
        # load the first epoch file to get the start time
        with open(epoch_files[0], "r") as f:
            epoch_data = json.load(f)
        epoch_start_time_str = epoch_data.get("time")
        if not epoch_start_time_str:
            log.error("Start time not found in the first epoch file.")
            return 1
        try:
            epoch_start_time = datetime.fromisoformat(epoch_start_time_str.replace("Z", "+00:00"))
        except ValueError:
            log.error("Invalid epoch start time format in the first epoch file.")
            return 1
        target_time = epoch_start_time + timedelta(seconds=args.offset_seconds)
    
    target_epoch_file = find_epoch_file_for_time(epoch_files, target_time)
    
    if not target_epoch_file:
        log.error("No epoch file found that covers the target time.")
        return 1
    log.info(f"Injecting commands into epoch file: {target_epoch_file} for target time: {target_time.isoformat()}")
    
    with open(target_epoch_file, "r") as f:
        epoch_data = json.load(f)
    try:
        commands_to_inject = parse_command_list(args.command_list)
    except ValueError as exc:
        log.error(str(exc))
        return 1
    if not commands_to_inject:
        log.error("No valid commands to inject.")
        return 1

    node_types = None
    if args.node_type_list:
        try:
            node_types = parse_command_list(args.node_type_list)
        except ValueError as exc:
            log.error(f"Invalid node type list: {exc}")
            return 1
        if not node_types:
            log.error("No valid node types provided in --node-type-list.")
            return 1
        if len(node_types) != len(commands_to_inject):
            log.error("--node-type-list must have the same number of entries as --command-list.")
            return 1

    if "run" not in epoch_data:
        epoch_data["run"] = {}
    if args.node:
        target_key = args.node
        if target_key not in epoch_data["run"]:
            epoch_data["run"][target_key] = []
        epoch_data["run"][target_key].extend(commands_to_inject)
    else:
        for cmd, node_type in zip(commands_to_inject, node_types):
            target_key = f"type:{node_type}"
            if target_key not in epoch_data["run"]:
                epoch_data["run"][target_key] = []
            epoch_data["run"][target_key].append(cmd)
    
    # copy the original epoch file to a backup before overwriting
    backup_epoch_file = target_epoch_file + ".bak"
    if not os.path.exists(backup_epoch_file):
        os.rename(target_epoch_file, backup_epoch_file)
        log.info(f"💾 Created backup of original epoch file at: {backup_epoch_file}")
    else:
        log.warning(f"⚠️ Backup epoch file already exists at: {backup_epoch_file}. Original file not backed up again.")
    with open(target_epoch_file, "w") as f:
        json.dump(epoch_data, f, indent=2)
    if args.node:
        log.info(f"✅ Successfully injected {len(commands_to_inject)} commands into epoch file '{target_epoch_file}' for target '{args.node}'")
    else:
        log.info(f" Successfully injected {len(commands_to_inject)} commands into epoch file '{target_epoch_file}' using per-command node types")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
