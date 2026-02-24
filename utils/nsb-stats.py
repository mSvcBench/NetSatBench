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
from datetime import datetime, timezone
from collections import deque, defaultdict

logging.basicConfig(level="INFO", format="[%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ==========================================
# GLOBALS
# ==========================================
node_map: Dict[str, int] = {}                        # name -> index


# ================================
# HELPERS
# ================================

def setup_logging(logfile=None, log_level=logging.INFO) -> logging.Logger:
    log = logging.getLogger()
    log.setLevel(logging.INFO)

    formatter = logging.Formatter(
        "[%(levelname)s] %(message)s"
    )

    # Console handler (always active)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    log.addHandler(console_handler)

    # Optional file handler
    if logfile:
        file_handler = logging.FileHandler(logfile, mode="a")
        file_handler.setFormatter(formatter)
        log.addHandler(file_handler)

    return log

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
        log.error(f"❌ JSON parse error in file: {path}")
        log.error(f"   - msg: {e.msg}")
        log.error(f"   - line: {e.lineno}, col: {e.colno}, char: {e.pos}")

        # Show a snippet around the error
        start = max(0, e.pos - 120)
        end = min(len(txt), e.pos + 120)
        snippet = txt[start:end].replace("\n", "\\n")
        log.error(f"   - context: ...{snippet}...")

        # Optional salvage: extract first {...} block and retry
        first = txt.find("{")
        last = txt.rfind("}")
        if 0 <= first < last:
            candidate = txt[first:last + 1]
            try:
                log.warning("⚠️  Attempting salvage parse by extracting outermost {...} block...")
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass

        raise

# ==========================================
# 2. STREAMING STATS FUNCTION
# ==========================================

def connected_components(
    num_nodes: int,
    active_links: Set[Tuple[int, int]],
    inv_node_map: Dict[int, str],
) -> List[Dict[str, List[str]]]:
    """
    Returns a list of connected components.
    Each component is represented as a dict:
        {
          "size": int,
          "nodes": [node_name_1, node_name_2, ...]
        }
    """
    adj = [[] for _ in range(num_nodes)]
    for a, b in active_links:
        adj[a].append(b)
        adj[b].append(a)

    visited = [False] * num_nodes
    components = []

    for s in range(num_nodes):
        if visited[s]:
            continue

        q = deque([s])
        visited[s] = True
        comp_nodes = []

        while q:
            u = q.popleft()
            comp_nodes.append(inv_node_map[u])
            for v in adj[u]:
                if not visited[v]:
                    visited[v] = True
                    q.append(v)

        components.append({
            "size": len(comp_nodes),
            "nodes": comp_nodes
        })

    return components

# ==========================================
# 3. METIS CLUSTERING (k-way partition)
# ==========================================
from typing import Iterable

def _build_weighted_metis_arrays(
    num_nodes: int,
    edge_weight: Dict[Tuple[int, int], int],
) -> Tuple[List[int], List[int], List[int]]:
    """
    Build (xadj, adjncy, eweights) in METIS CSR format for an undirected graph.

    edge_weight keys are undirected edges (min(i,j), max(i,j)) with positive integer weights.
    """
    neigh: List[List[Tuple[int, int]]] = [[] for _ in range(num_nodes)]

    for (a, b), w in edge_weight.items():
        if a == b:
            continue
        if w is None or w <= 0:
            continue
        neigh[a].append((b, int(w)))
        neigh[b].append((a, int(w)))

    # Sort neighbors for reproducibility
    for u in range(num_nodes):
        neigh[u].sort(key=lambda t: t[0])

    xadj: List[int] = [0]
    adjncy: List[int] = []
    eweights: List[int] = []
    for u in range(num_nodes):
        for v, w in neigh[u]:
            adjncy.append(v)
            eweights.append(w)
        xadj.append(len(adjncy))

    return xadj, adjncy, eweights


def metis_cluster_nodes(
    num_nodes: int,
    active_links: Iterable[Tuple[int, int]],
    inv_node_map: Dict[int, str],
    nparts: int,
    *,
    edge_weight: Optional[Dict[Tuple[int, int], int]] = None,
    contiguous: bool = False,
) -> Dict:
    """
    Partition nodes into 'nparts' groups with METIS, minimizing edge cuts.
    Returns a dict with:
      - cut: int
      - parts: List[int]   (len=num_nodes, part id per node)
      - groups: Dict[int, List[str]]  (part -> node names)

    If edge_weight is provided, it will be used as integer edge weights.
    Otherwise partitions the unweighted active_links graph.

    METIS objective: minimize cut edges => keep adjacent nodes together. :contentReference[oaicite:1]{index=1}
    """
    if nparts <= 1:
        parts = [0] * num_nodes
        return {"cut": 0, "parts": parts, "groups": {0: [inv_node_map[i] for i in range(num_nodes)]}}

    try:
        import pymetis
    except ImportError as e:
        raise SystemExit(
            "❌ pymetis is not installed. Install it with:\n"
            "   pip install pymetis\n"
            "or via conda-forge.\n"
        ) from e

    # If no weights given, create an unweighted edge_weight map from active_links
    if edge_weight is None:
        edge_weight = {}
        for a, b in active_links:
            if a == b:
                continue
            u, v = (a, b) if a < b else (b, a)
            edge_weight[(u, v)] = 1

    # If graph has no edges, METIS is not very meaningful; just split round-robin.
    if not edge_weight:
        parts = [i % nparts for i in range(num_nodes)]
        groups: Dict[int, List[str]] = defaultdict(list)
        for i, p in enumerate(parts):
            groups[p].append(inv_node_map[i])
        return {"cut": 0, "parts": parts, "groups": dict(groups)}

    xadj, adjncy, eweights = _build_weighted_metis_arrays(num_nodes, edge_weight)

    # pymetis.part_graph supports CSR + eweights (integer weights). :contentReference[oaicite:2]{index=2}
    cut, parts = pymetis.part_graph(
        nparts=nparts,
        xadj=xadj,
        adjncy=adjncy,
        eweights=eweights,
        contiguous=contiguous,
        # recursive left to pymetis default heuristic
    )

    groups: Dict[int, List[str]] = defaultdict(list)
    for i, p in enumerate(parts):
        groups[int(p)].append(inv_node_map[i])

    return {"cut": int(cut), "parts": list(map(int, parts)), "groups": dict(groups)}

def apply_metis_worker_assignment(
    in_json_path: str,
    out_json_path: str,
    node_to_part: Dict[str, int],
    workers_file: str = None
) -> None:
    """
    Updates a NetSatBench-style config JSON by assigning each node's `worker_field`
    according to its METIS partition id.

    Writes the updated JSON to out_json_path.

    Rules:
      - partition_id p is mapped to worker_names[p] (stable ordering)
      - if a node is missing from node_to_part -> error (fail-fast)
      - if p is outside range -> error
    """
    if workers_file is None:
        log.error("❌ workers_config must be provided to map partitions to workers.")
        exit(1)
    
    with open(in_json_path, "r", encoding="utf-8") as f:
        node_cfg: Dict[str, Any] = json.load(f)
    
    with open(workers_file, "r", encoding="utf-8") as f:
        workers_cfg: Dict[str, Any] = json.load(f)  
    
    worker_field = workers_cfg.get("workers")
    if not worker_field:
        log.error("❌ workers_config must contain a 'workers' field with worker names.")
        exit(1)
    
    workers = worker_field.keys()
    k = len(workers)
    workers = sorted(list(workers))

    log.info(f"🔎 Found {len(workers)} workers")

    # Validate partition ids
    for n, p in node_to_part.items():
        if not isinstance(p, int):
            raise ValueError(f"Partition id for node '{n}' is not int: {p!r}")
        if p < 0 or p >= k:
            raise ValueError(
                f"Partition id out of range for node '{n}': {p} (expected 0..{k-1})"
            )

    updated = 0
    missing = []

    block = node_cfg.get("nodes", {})
    if not isinstance(block, dict):
        raise ValueError(f"Section nodes must be a dict of node_name -> node_config.")

    for node_name, node_cfg in block.items():
        if node_name not in node_to_part:
            missing.append(node_name)
            continue
        if not isinstance(node_cfg, dict):
            raise ValueError(f"Node '{node_name}' in section 'nodes' must map to a dict.")

        part = node_to_part[node_name]
        node_cfg[worker_field] = workers[part]
        updated += 1

    if missing:
        raise KeyError(
            f"Missing METIS partition assignment for {len(missing)} node(s): {missing[:20]}"
            + (" ..." if len(missing) > 20 else "")
        )

    with open(out_json_path, "w", encoding="utf-8") as f:
        json.dump(node_cfg, f, indent=2, sort_keys=False)

    log.info(f"✅ Wrote updated config to {out_json_path} (assigned {updated} nodes, k={k} groups)")

def compute_streaming_stats(config_file: str, 
                            epoch_dir: str, 
                            file_pattern: str,
                            nclusters: int = 0,
                            cluster_weighted: bool = True,
                            cluster_contiguous: bool = False,
                            sat_config_out: Optional[str] = None,
                            workers_file: str = "worker-config.json",
                            ) -> None:

    # Load configurations
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            config = json.load(f)
        nodes = config.get("nodes", {})

    except FileNotFoundError:
        log.error(f"❌ Configuration file not found: {config_file}")
        raise
    except json.JSONDecodeError as e:
        log.error(f"❌ Invalid JSON in configuration file: {e}")
        raise

    log.info("📁 Loading configuration from files...")
    log.info(f"🔎 Found {len(nodes)} nodes")

    node_map = {}
    node_map.clear()
    idx = 0
    for name in nodes.keys():
        node_map[name] = idx; idx += 1

    inv_node_map = {i: name for name, i in node_map.items()}
    num_nodes = len(node_map)
    if num_nodes == 0:
        raise ValueError("Configuration has 0 nodes.")

    if not epoch_dir:
        epoch_dir = config.get("epoch-config", "").get("epoch-dir", "")
        if not epoch_dir:
            log.error("⚠️ No epoch directory specified via CLI or config file; skipping epoch stats.")
            return
    if not file_pattern:
        file_pattern = config.get("epoch-config", "").get("file-pattern", "")
        if not file_pattern:
            log.error("⚠️ No epoch file pattern specified via CLI or config file; skipping epoch stats.")
            return
    epoch_files = list_epoch_files(epoch_dir, file_pattern)
    log.info(f"🔎 Found {len(epoch_files)} epoch files to process.")
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
    churn_per_second: Dict[int, int] = defaultdict(int)  # floor(ts) -> churn events in that second
    first_ts = None
    last_ts = None

    # Counters for duration stats (don’t store all durations)
    duration_sum_sec = 0.0
    duration_count = 0
    duration_min = None
    duration_max = None
    partition_log: List[Dict] = []

    # For METIS clustering (time-weighted adjacency):
    # counts how many epochs each undirected edge was active
    edge_active_count: Dict[Tuple[int, int], int] = defaultdict(int)

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
        total_links_over_time += float(len(active_links))
        if (num_epochs>0): 
            # skip first epoch for churn stats (no prior state)
            total_churn += float(adds + dels)
            churn_per_second[int(ts)] += (adds + dels)
        num_epochs += 1

        # --- accumulate time-weighted adjacency (one count per epoch an edge is active)
        # TODO: change with epoch duration if epochs are irregularly spaced
        for key in active_links:
            edge_active_count[key] += 1

        # --- Connectivity check ---
        components = connected_components(num_nodes, active_links, inv_node_map)
        if len(components) > 1:
            partition_log.append({
                "time_iso": ts_raw,
                "time_sec": ts,
                "n_components": len(components),
                "components": components
            })
            log.warning(
                f"❌ PARTITION at {ts_raw}, file {path.split('/')[-1]}: "
                f"{len(components)} components "
                f"(largest={max(c['size'] for c in components)}/{num_nodes})"
                # print each component dict separated by "----"
                f"; details: \n----\n----\n" + "\n----\n".join(
                    f"size={c['size']}, nodes={c['nodes']} \n----" for c in components
                )
            )
        if num_epochs % 2000 == 0:
            log.info(f"… processed {num_epochs}/{len(epoch_files)} epochs; active_links={len(active_links)}")
        
    # ---- METIS clustering (optional; driven by CLI args passed down) ----
    if nclusters > 1:
        if cluster_weighted:
            weights = edge_active_count
            target_links = None
        else:
            weights = None
            target_links = edge_active_count.keys()
        res = metis_cluster_nodes(
            num_nodes=num_nodes,
            active_links=target_links,          # used only if weights=None
            inv_node_map=inv_node_map,
            nparts=nclusters,
            edge_weight=weights,
            contiguous=cluster_contiguous,
        )

        log.info("🧱 METIS Clustering (k-way partition):")
        log.info(f"   - k (groups): {nclusters}")
        log.info(f"   - edge-weighted over time: {bool(cluster_weighted)}")
        log.info(f"   - cut edges (objective): {res['cut']}")
        
        for gid in sorted(res["groups"].keys()):
            nodes = res["groups"][gid]
            log.info(f"   - group {gid:02d}: size={len(nodes)} nodes={nodes}")

        if sat_config_out:
            node_to_part = {inv_node_map[i]: p for i, p in enumerate(res["parts"])}
            apply_metis_worker_assignment(
                in_json_path=config_file,
                out_json_path=sat_config_out,
                node_to_part=node_to_part,
                workers_file=workers_file,
            )
    

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
        log.warning("⚠️ No epochs processed.")
        return

    avg_links_per_epoch = total_links_over_time / num_epochs
    avg_degree = (2.0 * avg_links_per_epoch) / num_nodes
    avg_churn = total_churn / num_epochs

    log.info("📊 Basic Statistics:")
    log.info(f"   - Number of epochs: {num_epochs}")
    log.info(f"   - Observation duration: "
             f"{(last_ts - first_ts) if first_ts is not None and last_ts is not None else 0:.2f} s")
    log.info(f"   - Number of nodes: {num_nodes}")
    log.info(f"   - Average links per epoch: {avg_links_per_epoch:.2f}")
    log.info(f"   - Average degree: {avg_degree:.2f}")
    log.info(f"   - Average link churn (add+del per epoch): {avg_churn:.2f}")
    # Observation duration in seconds
    if first_ts is None or last_ts is None or last_ts <= first_ts:
        obs_dur = 0.0
    else:
        obs_dur = float(last_ts - first_ts)

    avg_churn_per_sec = (total_churn / obs_dur) if obs_dur > 0 else float("nan")
    log.info(f"   - Average link churn (add+del per second): {avg_churn_per_sec:.4f}")
    if churn_per_second:
        series = np.array(list(churn_per_second.values()), dtype=float)
        log.info("📈 Churn-per-second summary (1s bins):")
        log.info(f"   - seconds observed (with churn events): {len(series)}")
        log.info(f"   - max churn/s: {series.max():.0f}")
        log.info(f"   - p95 churn/s: {np.percentile(series, 95):.0f}")
        log.info(f"   - p99 churn/s: {np.percentile(series, 99):.0f}")

    log.info("📊 Link Duration Statistics")
    if duration_count == 0:
        log.info("   - No link durations measured.")
    else:
        avg_dur = duration_sum_sec / duration_count
        log.info(f"   - Average link duration: {avg_dur:.2f} s")
        log.info(f"   - Min duration: {duration_min:.2f} s")
        log.info(f"   - Max duration: {duration_max:.2f} s")

    log.info("𐄳 Connectivity Statistics:")
    if not partition_log:
        log.info("   - Satellite system never partitioned.")
    else:
        log.info(f"   - Partitioned epochs: {len(partition_log)}/{num_epochs}")
        worst = max(partition_log, key=lambda e: e["n_components"])
        log.info(f"   - Maximum components observed: {worst['n_components']}")

def export_events_to_csv(config_file: str, epoch_dir: str, file_pattern: str, out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    # --- load config + build node_map (same as your logic) ---
    with open(config_file, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    nodes = cfg.get("nodes", {})
    node_map = {}
    idx = 0
    for name in nodes.keys():
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
        w.writerow(["t_sec", "t_iso", "src_id", "dst_id", "action", "rate", "delay", "loss","limit"])

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
                            l.get("rate", ""), l.get("delay", ""), l.get("loss", ""), l.get("limit", "")])

            # DEL
            for l in epoch.get("links-del", []):
                s = l.get("endpoint1"); d = l.get("endpoint2")
                if s not in node_map or d not in node_map:
                    continue
                w.writerow([t_sec, t_iso, node_map[s], node_map[d], "del",
                            "", "", "", ""])

            # UPDATE
            for l in epoch.get("links-update", []):
                s = l.get("endpoint1"); d = l.get("endpoint2")
                if s not in node_map or d not in node_map:
                    continue
                w.writerow([t_sec, t_iso, node_map[s], node_map[d], "update",
                            l.get("rate", ""), l.get("delay", ""), l.get("loss", ""), l.get("limit", "")])

    log.info(f"✅ Exported:\n  - {nodes_path}\n  - {events_path}")

# ==========================================
# MAIN
# ==========================================
def main() -> int:
    parser = argparse.ArgumentParser(description="Examine satellite system statistics")

    parser.add_argument(
        "-c", "--config",
        default="sat-config.json",
        help="Path to the JSON sat configuration file (e.g., sat-config.json)",
    )
    parser.add_argument(
        "-e", "--epoch-dir",
        default="",
        help="Directory containing epoch JSON files. Default is the dir included in the config file.",
    )
    parser.add_argument(
        "-p", "--file-pattern",
        default="",
        help="Epoch filename pattern. Default is the pattern included in the config file.",
    )
    parser.add_argument(
        "-w", "--worker-config",
        default=None,
        help="Path to the JSON worker configuration file.",
    )
    parser.add_argument(
        "--nclusters",
        type=int,
        default=0,
        help="If >1, run METIS to cluster nodes into this many groups (k-way partition).",
    )
    parser.add_argument(
        "--cluster-weighted",
        action="store_true",
        help="Use time-weighted edges (weight = number of epochs an edge was active).",
    )
    parser.add_argument(
        "--cluster-contiguous",
        action="store_true",
        help="Ask METIS for contiguous partitions (best-effort; not a hard guarantee).",
    )
    parser.add_argument(
        "--sat-config-out",
        type=str,default="sat-config-metis.json",
        help="If set, output config JSON with METIS worker assignment applied (no resource assurance !!).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (default: INFO)",
    )
    parser.add_argument(
        "--log-file",
        metavar="filename",
        help="Write log to file",
    )

    args = parser.parse_args()
    log = setup_logging(args.log_file, log_level=args.log_level.upper())
    
    compute_streaming_stats(
        config_file=args.config,
        epoch_dir=args.epoch_dir,
        file_pattern=args.file_pattern,
        nclusters=args.nclusters,
        cluster_weighted=args.cluster_weighted,
        cluster_contiguous=args.cluster_contiguous,
        sat_config_out=args.sat_config_out,
        workers_file=args.worker_config,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())