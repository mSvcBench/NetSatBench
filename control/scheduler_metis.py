#!/usr/bin/env python3
"""
scheduler_metis2.py  —  Edge-cut-focused hierarchical METIS scheduler
=====================================================================

This variant ignores METIS node weights entirely.

Goal
----
Partition the graph so that:
1. the number of links cut across groups is minimised, and
2. each group can be deployed on workers without violating CPU / MEM limits.

Only edge weights derived from epoch activity are used in METIS.
Resource values are used only for fit checks and worker assignment.
"""

import argparse
import copy
import json
import logging
import re
import os
import sys
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple
import pymetis
import scheduler as base_scheduler
from scheduler import parse_cpu, parse_mem
import json
from glob import glob

log = logging.getLogger("nsb-metis-scheduler")

def load_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        log.warning(f"⚠️  JSON error in {path}: {e}  — skipping file")
        return {}


def analyse_requirements(
    all_nodes: Dict[str, Any],
    workers: Dict[str, Any],
) -> Tuple[Dict[str, float], Dict[str, float]]:
    """
    Return CPU / MEM per node for worker-capacity accounting only.

    Zero-resource nodes keep CPU=0 and MEM=0 during placement.
    """

    node_cpu_raw: Dict[str, float] = {}
    node_mem_raw: Dict[str, float] = {}
    for name, cfg in all_nodes.items():
        node_cpu_raw[name] = parse_cpu(cfg.get("cpu-request")) or 0.0
        node_mem_raw[name] = parse_mem(cfg.get("mem-request")) or 0.0

    zero_resource_nodes: Set[str] = {
        n for n in all_nodes
        if node_cpu_raw[n] == 0.0 and node_mem_raw[n] == 0.0
    }

    if zero_resource_nodes:
        log.info(
            f"   Zero-resource nodes ({len(zero_resource_nodes)}): "
            "cpu=0.0000  mem=0.0000 GiB"
        )

    node_cpu: Dict[str, float] = dict(node_cpu_raw)
    node_mem: Dict[str, float] = dict(node_mem_raw)

    total_cpu_demand = sum(node_cpu.values())
    total_mem_demand = sum(node_mem.values())
    total_cpu_supply = sum(parse_cpu(w.get("cpu", 0)) for w in workers.values())
    total_mem_supply = sum(parse_mem(w.get("mem", 0)) for w in workers.values())

    log.info("🔍 Resource Analysis:")
    log.info(f"   Nodes: {len(all_nodes)}  (zero-resource: {len(zero_resource_nodes)})")
    log.info(
        f"   CPU  demand={total_cpu_demand:.2f}  supply={total_cpu_supply:.2f}  "
        f"{'✅' if total_cpu_supply >= total_cpu_demand else '⚠️  OVERCOMMIT'}"
    )
    log.info(
        f"   MEM  demand={total_mem_demand:.2f} GiB  supply={total_mem_supply:.2f} GiB  "
        f"{'✅' if total_mem_supply >= total_mem_demand else '⚠️  OVERCOMMIT'}"
    )

    return node_cpu, node_mem


def build_csr(
    node_indices: List[int],
    edge_weight: Dict[Tuple[int, int], int],
) -> Tuple[List[int], List[int], List[int]]:
    """Build pymetis CSR arrays for the subgraph induced by node_indices."""
    local = {g: l for l, g in enumerate(node_indices)}
    adj: List[List[Tuple[int, int]]] = [[] for _ in range(len(node_indices))]

    for (a, b), w in edge_weight.items():
        if a in local and b in local and w > 0:
            la, lb = local[a], local[b]
            adj[la].append((lb, int(w)))
            adj[lb].append((la, int(w)))

    for row in adj:
        row.sort()

    xadj, adjncy, ew = [0], [], []
    for row in adj:
        for v, w in row:
            adjncy.append(v)
            ew.append(w)
        xadj.append(len(adjncy))

    return xadj, adjncy, ew

def list_epoch_files(epoch_dir: str, file_pattern: str) -> List[str]:
    if not epoch_dir or not file_pattern:
        return []
    search_path = os.path.join(epoch_dir, file_pattern)

    def _last_num(p: str) -> int:
        nums = re.findall(r"(\d+)", os.path.basename(p))
        return int(nums[-1]) if nums else -1

    return sorted(glob(search_path), key=_last_num)

def build_links_weights(
    sat_config_data:  dict,
    epoch_dir:    str = "",
    file_pattern: str = "",
) -> Tuple[Dict[Tuple[int, int], int], Dict[str, int]]:
    """
    Stream all epoch JSON files and return:
        edge_active_count  {(min_idx, max_idx) -> n_epochs_active}
        node_activity      {node_name          -> total_active_link_epochs}

    """
    nodes: Dict[str, Any] = sat_config_data.get("nodes", {})
    epoch_cfg = sat_config_data.get("epoch-config", {})
    ep_dir = epoch_dir  or epoch_cfg.get("epoch-dir",    "")
    ep_pat = file_pattern or epoch_cfg.get("file-pattern", "")

    # Forward map  name → global index
    node_map: Dict[str, int] = {n: i for i, n in enumerate(nodes)}

    files = list_epoch_files(ep_dir, ep_pat)

    edge_cnt: Dict[Tuple[int, int], int] = defaultdict(int)
    active:   Set[Tuple[int, int]]       = set()   # currently-live edges

    if not files:
        log.warning(
            "⚠️  No epoch files found — METIS will use resource-only weights. "
            f"(epoch_dir={ep_dir!r}, pattern={ep_pat!r})"
        )
        return {}, {}

    for path in files:
        ep = load_json(path)

        # Apply links-add
        for lnk in ep.get("links-add", []):
            s, d = lnk.get("endpoint1"), lnk.get("endpoint2")
            if s not in node_map or d not in node_map or s == d:
                continue
            i, j = node_map[s], node_map[d]
            edge_key = (i, j) if i < j else (j, i)
            active.add(edge_key)

        # Apply links-del
        for lnk in ep.get("links-del", []):
            s, d = lnk.get("endpoint1"), lnk.get("endpoint2")
            if s not in node_map or d not in node_map or s == d:
                continue
            i, j = node_map[s], node_map[d]
            edge_key = (i, j) if i < j else (j, i)
            active.discard(edge_key)

        # Accumulate counts for this epoch snapshot
        for (i, j) in active:
            edge_cnt[(i, j)] += 1

    log.info(
        f"🪢 Epoch weights built: {len(files)} files, "
        f"{len(edge_cnt)} edges with activity, "
    )
    return dict(edge_cnt)

def pymetis_partition(
    node_indices: List[int],
    edge_weight: Dict[Tuple[int, int], int],
    nparts: int,
) -> List[int]:
    """Partition node_indices into nparts using edge weights only."""
    if nparts <= 1:
        return [0 for _ in node_indices]
    if len(node_indices) <= nparts:
        return list(range(len(node_indices)))

    xadj, adjncy, ew = build_csr(node_indices, edge_weight)
    if not adjncy:
        return [i % nparts for i in range(len(node_indices))]

    adjacency = pymetis.CSRAdjacency(xadj, adjncy)
    _, parts = pymetis.part_graph(
        nparts,
        adjacency=adjacency,
        eweights=ew,
    )
    return list(map(int, parts))


def hierarchical_metis_schedule(
    sat_config_data: Dict[str, Any],
    edge_active_count: Dict[Tuple[int, int], int],
    worker_config_data: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
    
    
    def free_cpu(wn: str) -> float:
        return worker_resource[wn]["cpu"] - worker_resource[wn]["cpu-used"]

    def free_mem(wn: str) -> float:
        return worker_resource[wn]["mem"] - worker_resource[wn]["mem-used"]

    def avail_cpu(wn: str) -> float:
        return worker_resource[wn]["cpu"] - worker_resource[wn]["cpu-used"]

    def avail_mem(wn: str) -> float:
        return worker_resource[wn]["mem"] - worker_resource[wn]["mem-used"]

    def worker_score(wn: str) -> float:
        return (
            avail_cpu(wn) + avail_mem(wn)/2.0 # rule of thumb 1 CPU 2 GiB for best balancing
        )
    
    def partition_score(group: List[str]) -> float:
        total_cpu = sum(node_cpu[n] for n in group)
        total_mem = sum(node_mem[n] for n in group)
        return total_cpu + total_mem / 2.0

    def assign(name: str, wn: str) -> None:
        all_nodes[name]["worker"] = wn
        worker_config_data_new[wn] = worker_resource[wn]["data"]
        new_cpu = parse_cpu(worker_config_data_new[wn].get("cpu-used", "0GiB")) + node_cpu[name]
        new_mem = parse_mem(worker_config_data_new[wn].get("mem-used", "0GiB")) + node_mem[name]
        worker_config_data_new[wn]["mem-used"] = f"{new_mem}GiB"
        worker_config_data_new[wn]["cpu-used"] = new_cpu
        log.info(f"    ➞ Assigned Node: {name} to Worker: {wn} (CPU Req: {node_cpu[name]}, MEM Req: {round(node_mem[name],4)}GiB)")
                
    
    sat_config_data_new = sat_config_data.copy()
    worker_config_data_new = worker_config_data.copy()
    all_nodes = sat_config_data_new.get("nodes", {})

    if not all_nodes:
        log.error("❌ No nodes in config.")
        sys.exit(1)
    if not worker_config_data_new:
        log.error("❌ No workers available.")
        sys.exit(1)

    configured_workers = [
        name for name, cfg in all_nodes.items()
        if cfg.get("worker") not in (None, "")
    ]
    if configured_workers:
        log.error(
            "❌ scheduler_metis does not accept nodes with a preconfigured "
            f"worker. Found: {', '.join(sorted(configured_workers))}"
        )
        sys.exit(1)

    node_cpu, node_mem = analyse_requirements(all_nodes, worker_config_data_new)

    combined_ew: Dict[Tuple[int, int], int] = {
        edge_key: max(1, int(v))
        for edge_key, v in edge_active_count.items()
    }

    node_map: Dict[str, int] = {n: i for i, n in enumerate(all_nodes)}
    inv_map: Dict[int, str] = {i: n for n, i in node_map.items()}

    worker_list = sorted(worker_config_data_new.keys())
    worker_resource: Dict[str, Dict[str, Any]] = {}
    for wn in worker_list:
        w_conf = worker_config_data_new[wn]
        sat_vnet_cidr = w_conf.get('sat-vnet-cidr', None)
        if not sat_vnet_cidr:
            log.error(f"❌ Worker {wn} missing 'sat-vnet-cidr' configuration. Cannot proceed with scheduling.")
            sys.exit(1)
        sat_vnet_cidr_mask = sat_vnet_cidr.split('/')[1]
        max_nodes = 2**(32 - int(sat_vnet_cidr_mask)) - 3  # reserve 5 IPs for network, gateway, broadcast, etc.
        worker_resource[wn] = {
            "cpu": parse_cpu(w_conf.get("cpu", 0)),
            "mem": parse_mem(w_conf.get("mem", 0)),
            "cpu-used": parse_cpu(w_conf.get("cpu-used", 0)),
            "mem-used": parse_mem(w_conf.get("mem-used", 0)),
            "max-nodes": max_nodes,
            "assigned-nodes": [],
            "data": w_conf,
        }

    all_metis_idx: List[int] = [node_map[n] for n in all_nodes if n in node_map]
    all_metis_names: List[str] = [inv_map[i] for i in all_metis_idx] # exact list of node names as ordered by metis idx

    for k in range(0,len(all_nodes)):
        # recursively split with metis untill all partition fits without overcommit, or we reach k=n_workers
        log.info(f"🧩 Trying METIS partitioning with {k+1} partitions...")
        partition_map = pymetis_partition(all_metis_idx, combined_ew, k+1) # list of partition id for the nodes
        trial_partitions: Dict[int, List[str]] = defaultdict(list)
        for name, part_id in zip(all_metis_names, partition_map):
            trial_partitions[part_id].append(name)

        all_fits = True
        partition_assignment: Dict[str, str] = {}
        temp_worker_resource = worker_resource.copy() # temp resource accounting for this trial partitioning
        for part_id , gnodes in sorted(
            trial_partitions.items(),
            key=lambda x: -partition_score(x[1]),
        ):
            cpu_needed = sum(node_cpu[n] for n in gnodes)
            mem_needed = sum(node_mem[n] for n in gnodes)
            placed = False
            for wn in sorted(temp_worker_resource, key=worker_score, reverse=True):
                avail_c = temp_worker_resource[wn]["cpu"] - temp_worker_resource[wn]["cpu-used"]
                avail_m = temp_worker_resource[wn]["mem"] - temp_worker_resource[wn]["mem-used"]
                if avail_c >= cpu_needed and avail_m >= mem_needed and len(temp_worker_resource[wn]["assigned-nodes"]) + len(gnodes) <= temp_worker_resource[wn]["max-nodes"]:
                    temp_worker_resource[wn]["cpu-used"] += cpu_needed
                    temp_worker_resource[wn]["mem-used"] += mem_needed
                    temp_worker_resource[wn]["assigned-nodes"].extend(gnodes)
                    placed = True
                    partition_assignment[str(part_id)] = wn
                    break
            if not placed:
                all_fits = False
                break
        
        if all_fits: 
            # assign nodes to workers according to partition_to_node_assignment
            worker_resource = temp_worker_resource # commit to this resource usage
            for name, part_id in zip(all_metis_names, partition_map):
                assigned_worker = partition_assignment[str(part_id)]
                assign(name, assigned_worker)
            log.info(f"✅ System fit successful with {k+1} partitions")
            return sat_config_data_new, worker_config_data_new
        
    ## Overcommit resort to plain scheduler
    sat_config_data_new, worker_config_data_new = base_scheduler.schedule_workers(sat_config_data_new, worker_config_data_new)
    return sat_config_data_new, worker_config_data_new


# ==========================================
#  SCHEDULING LOGIC
# ==========================================
def schedule_workers(sat_config_data: Dict[str, Any], workers_data: Dict[str, Any]) -> Dict[str, Any]:

    log.info("⚙️  Starting METIS scheduling logic...")
    edge_active_count = build_links_weights(
        sat_config_data=sat_config_data,
    )
    sat_config_data_new, worker_config_data_new = hierarchical_metis_schedule(
        sat_config_data=sat_config_data,
        edge_active_count=edge_active_count,
        worker_config_data=workers_data
    )
    return sat_config_data_new, worker_config_data_new


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Test the METIS scheduler.\n"
            "Reads sat-config.json + worker-config.json from disk and writes\n"
            "the resulting assignment to an output JSON file."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--sat-config", default="sat-config.json")
    parser.add_argument("--worker-config", default="worker-config.json")
    parser.add_argument("--output", default="scheduled_config.json")
    return parser
