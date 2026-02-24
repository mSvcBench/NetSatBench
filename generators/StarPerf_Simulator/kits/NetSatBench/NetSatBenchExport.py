#!/usr/bin/env python3
"""
Convert StarPerf 2.0 HDF5 extended matrices into sat-config.json and epoch event files in NetSatBench-style format
"""

import argparse
import json
import math
import os
import re
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Tuple, Any
import numpy as np
import h5py
import shutil


def parse_timeslot_index(name: str) -> int:
    """
    Convert 'timeslot12' -> 12
    """
    m = re.search(r"(\d+)$", name)
    if not m:
        raise ValueError(f"Unexpected timeslot dataset name: {name}")
    return int(m.group(1))



def is_link_present(delay_val: float) -> bool:
    """
    Decide if a matrix entry means 'link exists'.
    Adjust this if StarPerf uses a sentinel like -1 or a huge number.
    """
    if delay_val is None:
        return False
    if isinstance(delay_val, (float, np.floating)) and (math.isnan(delay_val) or math.isinf(delay_val)):
        return False
    # common sentinels you might see:
    if delay_val <= 0:
        return False
    return True


def rounding_function(unit_str: str):
    """
    Return a function that rounds a float to the nearest multiple of unit_str.
    For example, if unit_str is "0.1", then 12.34 -> 12.3, 12.36 -> 12.4.
    If unit_str is "1", then 12.34 -> 12, 12.56 -> 13.
    If unit_str is "0.01", then 12.345 -> 12.35, 12.344 -> 12.34.
    """
    try:
        unit = float(unit_str)
        if unit <= 0:
            raise ValueError
        def round_func(x):
            return round(x / unit) * unit
        return round_func
    except ValueError:
        raise ValueError(f"Invalid unit string: {unit_str}. Must be a positive number as a string (e.g., '1', '0.1', '0.01').")

def build_snapshot_data(delay_matrix: np.ndarray, rate_matrix: np.ndarray, loss_matrix: np.ndarray, delay_round_str: str, rate_round_str: str, loss_round_str: str) -> Dict[Tuple[int, int], Dict[str, Any]]:
    """
    Build a snapshot dict: (i,j) -> attrs for all existing undirected links.
    Only consider i<j to avoid duplicates.
    """
    n = delay_matrix.shape[0]
    snap: Dict[Tuple[int, int], Dict[str, Any]] = {}

    for i in range(n):
        for j in range(i + 1, n):
            d = float(delay_matrix[i, j])
            if is_link_present(d):
                d = float(delay_matrix[i, j] * 1000)  # convert to ms
                r = float(rate_matrix[i, j]) # in Mbps
                l = float(loss_matrix[i, j]) # as a float between 0 and 1
                d = rounding_function(delay_round_str)(d)
                r = rounding_function(rate_round_str)(r)
                l = rounding_function(str(float(loss_round_str)/100))(l)
                snap[(i, j)] = {"delay_ms": d, "rate_mbit": r, "loss": l}
    return snap


def diff_snapshots(
    prev: Dict[Tuple[int, int], Dict[str, Any]],
    curr: Dict[Tuple[int, int], Dict[str, Any]],
    node_name: List[str]
) -> Dict[str, List[Dict[str, Any]]]:
    prev_keys = set(prev.keys())
    curr_keys = set(curr.keys())  # if you want to consider small delay changes as link flaps, round before diffing

    added = curr_keys - prev_keys
    deleted = prev_keys - curr_keys
    common = prev_keys & curr_keys

    links_add = []
    for (i, j) in sorted(added):
        links_add.append({
            "endpoint1": node_name[i],
            "endpoint2": node_name[j],
            "rate": f"{curr[(i, j)]['rate_mbit']}mbit",
            "loss": f"{curr[(i, j)]['loss']}",
            "delay": f"{curr[(i, j)]['delay_ms']}ms",
        })

    links_del = []
    for (i, j) in sorted(deleted):
        # you can choose whether to emit the "old" delay or a constant placeholder
        links_del.append({
            "endpoint1": node_name[i],
            "endpoint2": node_name[j],
            "rate": 0,
            "loss": 0,
            "delay": 0,
        })

    links_update = []
    for (i, j) in sorted(common):
        old_delay = prev[(i, j)]["delay_ms"]
        new_delay = curr[(i, j)]["delay_ms"]
        old_rate = prev[(i, j)]["rate_mbit"]
        new_rate = curr[(i, j)]["rate_mbit"]
        old_loss = prev[(i, j)]["loss"]
        new_loss = curr[(i, j)]["loss"]


        # Only emit an update if the exported value changes
        # Update if one of the new value is different
        if (old_delay != new_delay) or (old_rate != new_rate) or (old_loss != new_loss):
            links_update.append({
                "endpoint1": node_name[i],
                "endpoint2": node_name[j],
                "rate": f"{curr[(i, j)]['rate_mbit']}mbit",
                "loss": f"{curr[(i, j)]['loss']}",
                "delay": f"{curr[(i, j)]['delay_ms']}ms",
            })


    return {"links-add": links_add, "links-del": links_del, "links-update": links_update}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--h5", required=True, help="HDF5 file with delay, position, type, rate, and loss datasets (e.g., output of NetSatBenchGenerate.py)")
    ap.add_argument("--outdir", default="../../examples/StarPerf", help="Directory to write sat-config.json and epoch files (default: ../../examples/StarPerf/, write in ../../examples/StarPerf/<constellation_name>)")
    ap.add_argument("--start-time-utc", default="2024-06-01T12:00:00Z",
                    help="Epoch0 'time' field (ISO-8601, UTC, Z suffix recommended), default '2024-06-01T12:00:00Z'")
    ap.add_argument("--delay-unit-ms",  default="1", help="Units of ms for rounding delay values in the output (e.g., default '1' for 1ms, '0.1' for 100us, 'microseconds' for 3 decimal places in ms)")
    ap.add_argument("--loss-unit-percent", default="1", help="Units of percent for rounding loss values in the output (e.g., default '1' for 1 percent, '0.1' for 0.1 percent)")
    ap.add_argument("--rate-unit-mbit", default="1", help="Units of Mbit for rounding rate values in the output (e.g., default '1' for 1 Mbit, '0.1' for 100 Kbit)")
    ap.add_argument("--sat-config-common", help="Path of the sat-config json file with only common node settings.")
    ap.add_argument("--shell", default="shell1", help="Only process this shell from the HDF5 file (default 'shell1')")
    args = ap.parse_args()
    

    # Basic validation of the HDF5 file structure
    with h5py.File(args.h5, "r") as f:
        if "delay" not in f:
            raise KeyError("HDF5 file has no 'delay' group. Check StarPerf connectivity output. "
                           "Interface convention expects per-timeslot delay datasets under 'delay/'.")
        if "position" not in f:
            raise KeyError("HDF5 file has no 'position' group. Check StarPerf connectivity output. "
                           "Interface convention expects per-timeslot position datasets under 'position/'.")
        if "type" not in f:
            raise KeyError("HDF5 file has no 'type' dataset. Check StarPerf connectivity output. "
                           "Interface convention expects 'type' dataset listing node types.")
        if "rate" not in f:
            raise KeyError("HDF5 file has no 'rate' dataset. Check StarPerf connectivity output. "
                           "Interface convention expects 'rate' dataset listing link rates.")
        if "loss" not in f:
            raise KeyError("HDF5 file has no 'loss' dataset. Check StarPerf connectivity output. "
                           "Interface convention expects 'loss' dataset listing link loss rates.")

        # h5 data retrieval based on the specified shell (default "shell1")
        shell_name=args.shell
        del_shell = f["delay"][shell_name]
        type_shell = f["type"][shell_name]["type"]
        loss_shell = f["loss"][shell_name]
        rate_shell = f["rate"][shell_name]
        info_group = f["info"]
        
        print(f"🛰️ Processing HDF5 file {args.h5} for shell {shell_name}.")
        
        # print all attributes in info group
        print("  Info attributes:")
        for ts, v in info_group.attrs.items():
            print(f"    {ts}: {v}")
    
        # ask to clean outdir if not empty
        os.makedirs(args.outdir, exist_ok=True)
        if os.listdir(args.outdir):
            print(f"⚠️ Warning: output directory {args.outdir} is not empty.")
            response = input("  Do you want to continue remove all files? (y/n): ")
            if response.lower() == 'y':
                #remove the whole directory and recreate it
                #force remove the directory itself to ensure all files are deleted, then recreate it
                shutil.rmtree(args.outdir)
                print(f"  Emptied directory {args.outdir}.")
                os.makedirs(args.outdir, exist_ok=True)
        
        
        # parse start time
        start_str = args.start_time_utc.replace("Z", "+00:00")
        t0 = datetime.fromisoformat(start_str).astimezone(timezone.utc)
        dT = float(info_group.attrs.get("dT", 1.0))  # default to 1 second if not specified
        constellation_name = info_group.attrs.get("constellation_name", "unknown_constellation")
        timeslot_names = sorted(del_shell.keys(), key=parse_timeslot_index)
        os.makedirs(args.outdir+"/"+constellation_name, exist_ok=True)
        os.makedirs(args.outdir+"/"+constellation_name+"/constellation-epochs", exist_ok=True)
        
        # build node name mapping from type
        n_nodes = [-1, -1, -1]  # satellite, gateway, user
        node_name = [""] * (len(type_shell))  # 1-based indexing
        for i, t in enumerate(type_shell):
            t_str = t.decode("utf-8")
            if t_str == "sat":
                n_nodes[0] += 1
                node_name[i] = f"sat{n_nodes[0]}"
            elif t_str == "gs":
                n_nodes[1] += 1
                node_name[i] = f"gs{n_nodes[1]}"
            elif t_str == "user":
                n_nodes[2] += 1
                node_name[i] = f"usr{n_nodes[2]}"
        
        # prepare the sat-config.json file (optional)
        if args.sat_config_common:
            sat_config_common = {}
            with open(args.sat_config_common, "r", encoding="utf-8") as r:
                sat_config_common = json.load(r)
            if "nodes" in sat_config_common:
                del sat_config_common["nodes"]
            sat_config_common["nodes"] = {}
            for i, nn in enumerate(node_name):
                sat_config_common["nodes"][nn] = {}
                sat_config_common["nodes"][nn]["name"] = nn
                type_str = type_shell[i].decode("utf-8")
                if type_str == "gs":
                    sat_config_common["nodes"][nn]["type"] = "gateway"
                elif type_str == "sat":
                    sat_config_common["nodes"][nn]["type"] = "satellite"
                else:
                    sat_config_common["nodes"][nn]["type"] = type_str  # e.g., "user"
                
            #find absolute path from args.outdir
            absolute_outdir_path = os.path.abspath(args.outdir)
            sat_config_common["epoch-config"] = {
                "epoch-dir": f"{absolute_outdir_path}/{constellation_name}/epochs",
                "file-pattern": "NetSatBench-epoch*.json"
            }
            with open(f"{absolute_outdir_path}/{constellation_name}/sat-config.json", "w", encoding="utf-8") as w:
                json.dump(sat_config_common, w, indent=2)
            print(f"💾 Wrote satellite configuration file to f'{absolute_outdir_path}/{constellation_name}/sat-config.json")
        
        prev_snap = None
        for ts, ts_name in enumerate(timeslot_names):
            delay_matrix = np.array(del_shell[ts_name])
            rate_matrix = np.array(rate_shell[ts_name])
            loss_matrix = np.array(loss_shell[ts_name])

            curr_snap = build_snapshot_data(delay_matrix, rate_matrix, loss_matrix, args.delay_unit_ms, args.rate_unit_mbit, args.loss_unit_percent)

            if prev_snap is None:
                # epoch0: treat everything as "add" (common for event logs)
                delta = diff_snapshots({}, curr_snap, node_name)
            else:
                delta = diff_snapshots(prev_snap, curr_snap, node_name)

            epoch_time = t0 + timedelta(seconds=ts * dT)
            epoch_obj = {
                "time": epoch_time.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                "links-del": delta["links-del"],
                "links-update": delta["links-update"],
                "links-add": delta["links-add"],
                # "run": {...}  # keep empty unless you want to inject commands
            }

            out_path = os.path.join(args.outdir+"/"+constellation_name+"/epochs", f"NetSatBench-epoch{ts}.json")
            with open(out_path, "w", encoding="utf-8") as w:
                json.dump(epoch_obj, w, indent=2)

            prev_snap = curr_snap

    print(f"💾 Wrote {len(timeslot_names)} epoch files to {args.outdir}/constellation-epochs")


if __name__ == "__main__":
    main()
