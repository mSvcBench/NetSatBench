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
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timezone

# ==========================================
# GLOBALS
# ==========================================
node_map: Dict[str, int] = {}                        # name -> index


# ================================
# HELPERS
# ================================
def list_epoch_files(epoch_dir: str, file_pattern: str) -> List[str]:
    if not epoch_dir or not file_pattern:
        return []

    search_path = os.path.join(epoch_dir, file_pattern)

    def last_numeric_suffix(path: str) -> int:
        basename = os.path.basename(path)
        matches = re.findall(r"(\d+)", basename)
        return int(matches[-1]) if matches else -1

    return sorted(glob(search_path), key=last_numeric_suffix)


def parse_utc_timestamp(ts: str) -> float:
    """
    Convert ISO-8601 UTC timestamp (e.g. '2025-12-01T00:00:00Z')
    to seconds since epoch.
    """
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()

import calendar
import time
from typing import Dict, Tuple, Set

def convert_time_epoch_to_timestamp(time_str: str) -> float:
    """
    Converts ISO-8601 UTC 'YYYY-MM-DDTHH:MM:SSZ' to Unix timestamp (UTC).
    """
    try:
        st = time.strptime(time_str, "%Y-%m-%dT%H:%M:%SZ")
        return float(calendar.timegm(st))
    except ValueError:
        raise ValueError(
            f"❌ Invalid time format: {time_str}. Expected 'YYYY-MM-DDTHH:MM:SSZ'."
        )

def load_json_file_or_report(path: str) -> dict:
    """
    Loads JSON and prints a helpful error with context if the file is malformed.
    Also tries a very small 'salvage' for common cases: extra junk before/after JSON.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    except json.JSONDecodeError as e:
        # Read raw text to show context
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            txt = f.read()

        # Print precise position
        print(f"❌ JSON parse error in file: {path}")
        print(f"   - msg: {e.msg}")
        print(f"   - line: {e.lineno}, col: {e.colno}, char: {e.pos}")

        # Show a snippet around the error
        start = max(0, e.pos - 120)
        end = min(len(txt), e.pos + 120)
        snippet = txt[start:end].replace("\n", "\\n")
        print(f"   - context: ...{snippet}...")

        # Optional salvage: extract first {...} block and retry
        first = txt.find("{")
        last = txt.rfind("}")
        if 0 <= first < last:
            candidate = txt[first:last + 1]
            try:
                print("⚠️  Attempting salvage parse by extracting outermost {...} block...")
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass

        raise

# ==========================================
# 2. STREAMING STATS FUNCTION
# ==========================================
 
def compute_streaming_stats(config_file: str, epoch_dir: str, file_pattern: str) -> None:
    # Load configuration and build node_map (same logic you already have)
    with open(config_file, "r", encoding="utf-8") as f:
        config = json.load(f)

    satellites = config.get("satellites", {})
    users = config.get("users", {})
    grounds = config.get("grounds", {})

    print("📁 Loading configuration from file...")
    print(f"🔎 Found {len(satellites)} satellites, {len(users)} users, {len(grounds)} ground stations in configuration.")

    node_map.clear()
    idx = 0
    for name in satellites.keys():
        node_map[name] = idx; idx += 1
    for name in users.keys():
        node_map[name] = idx; idx += 1
    for name in grounds.keys():
        node_map[name] = idx; idx += 1

    num_nodes = len(node_map)
    if num_nodes == 0:
        raise ValueError("Configuration has 0 nodes.")

    epoch_files = list_epoch_files(epoch_dir, file_pattern)
    print(f"🔎 Found {len(epoch_files)} epoch files to process.")
    if not epoch_files:
        raise FileNotFoundError(f"No epoch files found in '{epoch_dir}' with pattern '{file_pattern}'")

    # ---- Streaming state ----
    # Active undirected links as (min(i,j), max(i,j))
    active_links: Set[Tuple[int, int]] = set()

    # For link duration: start timestamp for currently active links
    link_start_time: Dict[Tuple[int, int], float] = {}

    # Counters for basic stats
    num_epochs = 0
    total_links_over_time = 0.0          # sum of active link count each epoch
    total_churn = 0.0                    # adds+removes per epoch (undirected)
    first_ts = None
    last_ts = None

    # Counters for duration stats (don’t store all durations)
    duration_sum_sec = 0.0
    duration_count = 0
    duration_min = None
    duration_max = None

    for path in epoch_files:
        epoch_data = load_json_file_or_report(path)
        ts_raw = epoch_data.get("time")
        if ts_raw is None:
            continue
        ts = convert_time_epoch_to_timestamp(ts_raw)

        if first_ts is None:
            first_ts = ts
        last_ts = ts

        # Apply link-add
        adds = 0
        for link_add in epoch_data.get("links-add", []):
            src = link_add.get("endpoint1")
            dst = link_add.get("endpoint2")
            if src not in node_map or dst not in node_map:
                continue
            i = node_map[src]
            j = node_map[dst]
            if i == j:
                continue
            a, b = (i, j) if i < j else (j, i)
            key = (a, b)

            if key not in active_links:
                active_links.add(key)
                link_start_time[key] = ts
                adds += 1

        # Apply link-del
        dels = 0
        for link_del in epoch_data.get("links-del", []):
            src = link_del.get("endpoint1")
            dst = link_del.get("endpoint2")
            if src not in node_map or dst not in node_map:
                continue
            i = node_map[src]
            j = node_map[dst]
            if i == j:
                continue
            a, b = (i, j) if i < j else (j, i)
            key = (a, b)

            if key in active_links:
                active_links.remove(key)
                dels += 1

                # Close duration
                start = link_start_time.pop(key, None)
                if start is not None:
                    dt = ts - start
                    if dt > 0:
                        duration_sum_sec += dt
                        duration_count += 1
                        duration_min = dt if duration_min is None else min(duration_min, dt)
                        duration_max = dt if duration_max is None else max(duration_max, dt)

        # Update per-epoch stats
        num_epochs += 1
        total_links_over_time += float(len(active_links))
        total_churn += float(adds + dels)

        if num_epochs % 2000 == 0:
            print(f"… processed {num_epochs}/{len(epoch_files)} epochs; active_links={len(active_links)}")

    # Close durations for links still active at the end
    if last_ts is not None:
        for key, start in link_start_time.items():
            dt = last_ts - start
            if dt > 0:
                duration_sum_sec += dt
                duration_count += 1
                duration_min = dt if duration_min is None else min(duration_min, dt)
                duration_max = dt if duration_max is None else max(duration_max, dt)

    # ---- Print results ----
    if num_epochs == 0:
        print("⚠️ No epochs processed.")
        return

    avg_links_per_epoch = total_links_over_time / num_epochs
    avg_degree = (2.0 * avg_links_per_epoch) / num_nodes
    avg_churn = total_churn / num_epochs

    print("\n📊 Basic Statistics (streaming):")
    print(f"   - Number of epochs: {num_epochs}")
    print(f"   - Number of nodes: {num_nodes}")
    print(f"   - Average links per epoch: {avg_links_per_epoch:.2f}")
    print(f"   - Average degree: {avg_degree:.2f}")
    print(f"   - Average link churn (add+del per epoch): {avg_churn:.2f}")

    print("\n📊 Link Duration Statistics (seconds, streaming):")
    if duration_count == 0:
        print("   - No link durations measured.")
    else:
        avg_dur = duration_sum_sec / duration_count
        print(f"   - Number of link lifetimes: {duration_count}")
        print(f"   - Average link duration: {avg_dur:.2f} s")
        print(f"   - Min duration: {duration_min:.2f} s")
        print(f"   - Max duration: {duration_max:.2f} s")

def export_events_to_csv(config_file: str, epoch_dir: str, file_pattern: str, out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)

    # --- load config + build node_map (same as your logic) ---
    with open(config_file, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    satellites = cfg.get("satellites", {})
    users = cfg.get("users", {})
    grounds = cfg.get("grounds", {})

    node_map = {}
    idx = 0
    for name in satellites.keys():
        node_map[name] = idx; idx += 1
    for name in users.keys():
        node_map[name] = idx; idx += 1
    for name in grounds.keys():
        node_map[name] = idx; idx += 1

    # write node map
    nodes_path = os.path.join(out_dir, "nodes.csv")
    with open(nodes_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["node_id", "node_name"])
        for name, i in sorted(node_map.items(), key=lambda kv: kv[1]):
            w.writerow([i, name])

    events_path = os.path.join(out_dir, "link_events.csv")
    with open(events_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["t_sec", "t_iso", "src_id", "dst_id", "action", "rate", "delay", "loss"])

        files = list_epoch_files(epoch_dir, file_pattern)
        for path in files:
            epoch = load_json_file_or_report(path)  # your robust loader
            t_iso = epoch.get("time")
            if not t_iso:
                continue
            t_sec = convert_time_epoch_to_timestamp(t_iso)

            # ADD
            for l in epoch.get("links-add", []):
                s = l.get("endpoint1"); d = l.get("endpoint2")
                if s not in node_map or d not in node_map:
                    continue
                w.writerow([t_sec, t_iso, node_map[s], node_map[d], "add",
                            l.get("rate", ""), l.get("delay", ""), l.get("loss", "")])

            # DEL
            for l in epoch.get("links-del", []):
                s = l.get("endpoint1"); d = l.get("endpoint2")
                if s not in node_map or d not in node_map:
                    continue
                w.writerow([t_sec, t_iso, node_map[s], node_map[d], "del",
                            "", "", ""])

            # UPDATE
            for l in epoch.get("links-update", []):
                s = l.get("endpoint1"); d = l.get("endpoint2")
                if s not in node_map or d not in node_map:
                    continue
                w.writerow([t_sec, t_iso, node_map[s], node_map[d], "update",
                            l.get("rate", ""), l.get("delay", ""), l.get("loss", "")])

    print(f"✅ Exported:\n  - {nodes_path}\n  - {events_path}")
# ==========================================
# MAIN
# ==========================================
def main() -> int:
    parser = argparse.ArgumentParser(description="Examine constellation statistics")

    parser.add_argument(
        "-c", "--config",
        default="config.json",
        help="Path to the JSON configuration file (e.g., config.json)",
    )
    parser.add_argument(
        "-e", "--epoch-dir",
        default="constellation-epochs/",
        help="Directory containing epoch JSON files.",
    )
    parser.add_argument(
        "-p", "--file-pattern",
        default="NetSatBench-epoch*.json",
        help="Epoch filename glob pattern (inside epoch-dir).",
    )

    args = parser.parse_args()

    compute_streaming_stats(
        config_file=args.config,
        epoch_dir=args.epoch_dir,
        file_pattern=args.file_pattern,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())