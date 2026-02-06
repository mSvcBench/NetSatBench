#!/usr/bin/env python3
"""
Convert StarPerf 2.0 c:contentReference[oaicite:8]{index=8} (HDF5 delay matrices per timeslot)
into epoch event files in NetSatBench-style format:
  { time, links-del, links-add, links-update, run? }

StarPerf notes:
- StarPerf supports Grid / Grid+ fixed-neighbor ISL patterns. :contentReference[oaicite:9]{index=9}
- Its interface convention describes writing per-timeslot delay matrices to HDF5. :contentReference[oaicite:10]{index=10}
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


def delay_to_ms_string(delay_seconds: float, round_units: str) -> str:
    ms = delay_seconds * 1000.0
    if round_units == "microseconds":
        return f"{ms:.3f}ms"
    if round_units == "ms":
       return f"{ms:.0f}ms"
    else:
        return f"{ms:.0f}ms"

def build_snapshot(delay_matrix: np.ndarray) -> Dict[Tuple[int, int], Dict[str, Any]]:
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
                snap[(i, j)] = {"delay_s": d}
    return snap


def diff_snapshots(
    prev: Dict[Tuple[int, int], Dict[str, Any]],
    curr: Dict[Tuple[int, int], Dict[str, Any]],
    rate: Dict[str, str],
    loss: Dict[str, float],
    round_units: str,
    node_name: List[str]
) -> Dict[str, List[Dict[str, Any]]]:
    prev_keys = set(prev.keys())
    curr_keys = set(curr.keys())  # if you want to consider small delay changes as link flaps, round before diffing

    added = curr_keys - prev_keys
    deleted = prev_keys - curr_keys
    common = prev_keys & curr_keys

    links_add = []
    for (i, j) in sorted(added):
        link_type = "unknown"
        if node_name[i].startswith("sat") and node_name[j].startswith("sat"):
            link_type = "isl"
        elif (node_name[i].startswith("sat") and node_name[j].startswith("gs")) or (node_name[i].startswith("gs") and node_name[j].startswith("sat")):
            link_type = "gs"
        elif (node_name[i].startswith("sat") and node_name[j].startswith("usr")) or (node_name[i].startswith("usr") and node_name[j].startswith("sat")):
            link_type = "user"
        links_add.append({
            "endpoint1": node_name[i],
            "endpoint2": node_name[j],
            "rate": rate[link_type],
            "loss": loss[link_type],
            "delay": delay_to_ms_string(curr[(i, j)]["delay_s"],round_units),
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
        old_d = prev[(i, j)]["delay_s"]
        new_d = curr[(i, j)]["delay_s"]

        old_s = delay_to_ms_string(old_d, round_units)
        new_s = delay_to_ms_string(new_d, round_units)

        # Only emit an update if the exported value changes
        if old_s != new_s:
            link_type = "unknown"
            if node_name[i].startswith("sat") and node_name[j].startswith("sat"):
                link_type = "isl"
            elif (node_name[i].startswith("sat") and node_name[j].startswith("gs")) or (node_name[i].startswith("gs") and node_name[j].startswith("sat")):
                link_type = "gs"
            elif (node_name[i].startswith("sat") and node_name[j].startswith("usr")) or (node_name[i].startswith("usr") and node_name[j].startswith("sat")):
                link_type = "user"
            links_update.append({
                "endpoint1": node_name[i],
                "endpoint2": node_name[j],
                "rate": rate[link_type],
                "loss": loss[link_type],
                "delay": new_s,
            })


    return {"links-add": links_add, "links-del": links_del, "links-update": links_update}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--h5", required=True, help="StarPerf output HDF5 file containing for NEtSatBench processing.")
    ap.add_argument("--outdir", default="../../examples/StarPerf", help="Directory to write sat-config.json and epoch files (default: ../../examples/StarPerf)")
    ap.add_argument("--epoch-seconds", type=float, default=15.0, help="Time step between timeslots")
    ap.add_argument("--start-time-utc", default="2024-06-01T12:00:00Z",
                    help="Epoch0 'time' field (ISO-8601, UTC, Z suffix recommended)")
    ap.add_argument("--delay-round",  choices=["microseconds", "ms"], default="ms", help="Rounding unit for delay values (default: ms).")
    ap.add_argument("--isl-rate", default="100mbit", help="Rate of ISL links (default: 100mbit)")
    ap.add_argument("--gs-rate", default="100mbit", help="Rate of Sat to Ground Station (Gateway) Links (default: 100mbit)")
    ap.add_argument("--user-rate", default="50mbit", help="Rate of Sat to User links (default: 50mbit)")
    ap.add_argument("--loss-isl", type=float, default=0.0, help="Loss rate for ISL links (default: 0.0)")
    ap.add_argument("--loss-gs", type=float, default=0.0, help="Loss rate for Sat to Ground Station (Gateway) Links (default: 0.0)")
    ap.add_argument("--loss-user", type=float, default=0.0, help="Loss rate for Sat to User links (default: 0.0)")
    ap.add_argument("--sat-config-common", help="Path of the sat-config json file with only common node settings.")
    args = ap.parse_args()
    rate = {
        "isl": args.isl_rate,
        "gs": args.gs_rate,
        "user": args.user_rate,
    }
    loss = {
        "isl": args.loss_isl,
        "gs": args.loss_gs,
        "user": args.loss_user,
    }
    os.makedirs(args.outdir, exist_ok=True)
    # ask to clean outdir if not empty
    if os.listdir(args.outdir):
        print(f"⚠️ Warning: output directory {args.outdir} is not empty.")
        response = input("  Do you want to continue remove all files? (y/n): ")
        if response.lower() == 'y':
            #remove the whole directory and recreate it
            #force remove the directory itself to ensure all files are deleted, then recreate it
            shutil.rmtree(args.outdir)
            print(f"  Emptied directory {args.outdir}.")
            os.makedirs(args.outdir, exist_ok=True)
    
    os.makedirs(args.outdir+"/constellation-epochs", exist_ok=True)

    # parse start time
    start_str = args.start_time_utc.replace("Z", "+00:00")
    t0 = datetime.fromisoformat(start_str).astimezone(timezone.utc)

    with h5py.File(args.h5, "r") as f:
        if "delay_nsb" not in f:
            raise KeyError("HDF5 file has no 'delay_nsb' group. Check StarPerf connectivity output. "
                           "Interface convention expects per-timeslot delay datasets under 'delay_nsb/'.")
        if "position_nsb" not in f:
            raise KeyError("HDF5 file has no 'position_nsb' group. Check StarPerf connectivity output. "
                           "Interface convention expects per-timeslot position datasets under 'position_nsb/'.")
        if "type_nsb" not in f:
            raise KeyError("HDF5 file has no 'type_nsb' dataset. Check StarPerf connectivity output. "
                           "Interface convention expects 'type_nsb' dataset listing node types.")

        s_name="shell1"  # assuming single shell named 'shell1' for simplicity
        g = f["delay_nsb"]
        s = g[s_name]
        gt= f["type_nsb"]
        st = gt[s_name]["type_nsb"]
        timeslot_names = sorted(s.keys(), key=parse_timeslot_index)
        # build node name mapping from type_nsb
        n_nodes = [-1, -1, -1]  # satellite, gateway, user
        node_name = [""] * (len(st))  # 1-based indexing
        for i, t in enumerate(st):
            t_str = t.decode("utf-8")
            if t_str == "satellite":
                n_nodes[0] += 1
                node_name[i] = f"sat{n_nodes[0]}"
            elif t_str == "gateway":
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
                sat_config_common["nodes"][nn]["type"] = st[i].decode("utf-8")

            with open(args.outdir + "/sat-config.json", "w", encoding="utf-8") as w:
                json.dump(sat_config_common, w, indent=2)
            print(f"💾 Wrote satellite configuration file to {args.outdir}/sat-config.json")
        
        prev_snap = None
        for k, ds_name in enumerate(timeslot_names):
            delay_matrix = np.array(s[ds_name])
            # remove first raw because left empty from StarPerf convention (sat indices 0..N-1 mapped to 1..N)
            delay_matrix = delay_matrix[1:, 1:]
            curr_snap = build_snapshot(delay_matrix)

            if prev_snap is None:
                # epoch0: treat everything as "add" (common for event logs)
                delta = diff_snapshots({}, curr_snap, rate, loss, args.delay_round, node_name)
            else:
                delta = diff_snapshots(prev_snap, curr_snap, rate, loss, args.delay_round, node_name)

            epoch_time = t0 + timedelta(seconds=k * args.epoch_seconds)
            epoch_obj = {
                "time": epoch_time.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                "links-del": delta["links-del"],
                "links-update": delta["links-update"],
                "links-add": delta["links-add"],
                # "run": {...}  # keep empty unless you want to inject commands
            }

            out_path = os.path.join(args.outdir+"/constellation-epochs", f"NetSatBench-epoch{k}.json")
            with open(out_path, "w", encoding="utf-8") as w:
                json.dump(epoch_obj, w, indent=2)

            prev_snap = curr_snap

    print(f"💾 Wrote {len(timeslot_names)} epoch files to {args.outdir}/constellation-epochs")


if __name__ == "__main__":
    main()
