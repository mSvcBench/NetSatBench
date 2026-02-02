#!/usr/bin/env python3

import calendar
import time
from typing import Any, Dict, List, Tuple, Set
import os
import re
import json
import argparse
from glob import glob
from datetime import datetime, timedelta
from collections import defaultdict
from typing import List, Optional
import etcd3
import sys
import logging
from scipy.sparse import lil_matrix, csr_matrix
# (optional, but recommended for dijkstra)
from scipy.sparse.csgraph import dijkstra

logging.basicConfig(level="INFO", format="[%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ==========================================
# ETCD CONNECTION
# ==========================================
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

# ================================
# HELPERS
# ================================
def last_numeric_suffix(path: str) -> int:
        basename = os.path.basename(path)
        matches = re.findall(r"(\d+)", basename)
        return int(matches[-1]) if matches else -1
def list_epoch_files(epoch_dir: str, file_pattern: str) -> List[str]:
    if not epoch_dir or not file_pattern:
        return []
    search_path = os.path.join(epoch_dir, file_pattern)
    return sorted(glob(search_path), key=last_numeric_suffix)


def parse_utc_timestamp(ts: str) -> float:
    """
    Convert ISO-8601 UTC timestamp (e.g. '2025-12-01T00:00:00Z')
    to seconds since epoch.
    """
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


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

def load_epoch_dir_and_pattern_from_etcd(etcd_client) -> Tuple[str, str]:
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
    
def get_prefix_data(etcd, prefix: str) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for value, metadata in etcd.get_prefix(prefix):
        key = metadata.key.decode('utf-8').split('/')[-1]
        try:
            data[key] = json.loads(value.decode('utf-8'))
        except json.JSONDecodeError:
            log.warning(f"⚠️ Warning: Could not parse JSON for key {key} under {prefix}")
    return data

# ==========================================
# ROUTE COMPUTATION LOGIC
# ==========================================

def pick_primary_secondary_next_hops(A_csr: csr_matrix, dist, src_idx: int, target_idx: int) -> list[int]:
    """
    Returns [primary_nh] or [primary_nh, secondary_nh].
    Primary = shortest path.
    Secondary = shortest path among those whose first hop != primary (may be longer).
    """
    d_st = dist[src_idx, target_idx]
    if d_st == float("inf") or src_idx == target_idx:
        return []

    # neighbors of src in CSR
    row_start = A_csr.indptr[src_idx]
    row_end = A_csr.indptr[src_idx + 1]
    neighbors = A_csr.indices[row_start:row_end]

    cands = []
    for n in neighbors:
        d_nt = dist[n, target_idx]
        if d_nt == float("inf"):
            continue
        # cost of best path constrained to start with src->n
        cands.append((1 + d_nt, n))

    if not cands:
        return []

    cands.sort(key=lambda x: (x[0], x[1]))  # deterministic
    primary = cands[0][1]

    secondary = None
    for _, n in cands[1:]:
        if n != primary:
            secondary = n
            break

    return [primary] if secondary is None else [primary, secondary]

def compute_routes_single_epoch( 
                   epoch_data: dict, 
                   node_map: dict, 
                   A_lil: csr_matrix, 
                   node_to_route: list, 
                   previous_next_hops: list, 
                   drain_before_break: bool,
                   offset_seconds: int,
                   num_nodes: int,
                   inv_node_map: dict,
                   ip_map: dict
                   ) -> dict:
    
    # Apply link-add only if is not a drain-before-break epoch
    if drain_before_break == False:
        for link_add in epoch_data.get("links-add", []):
            src = link_add.get("endpoint1")
            dst = link_add.get("endpoint2")
            if src not in node_map or dst not in node_map:
                continue
            i = node_map[src]
            j = node_map[dst]
            if i == j:
                continue
            # only count if it was previously absent
            if A_lil[i, j] == 0:
                A_lil[i, j] = 1
                A_lil[j, i] = 1

    # Apply link-del
    for link_del in epoch_data.get("links-del", []):
        src = link_del.get("endpoint1")
        dst = link_del.get("endpoint2")
        if src not in node_map or dst not in node_map:
            continue
        i = node_map[src]
        j = node_map[dst]
        if i == j:
            continue

        # only count if it was previously present
        if A_lil[i, j] != 0:
            A_lil[i, j] = 0
            A_lil[j, i] = 0

    # ---------------------------
    # Compute Dijkstra on sparse A
    # For unweighted links: treat each edge weight as 1
    # Build CSR for efficient shortest path
    # ---------------------------
    A_csr: csr_matrix = A_lil.tocsr()

    # If your adjacency is 0/1 and you want hop-count distances,
    # convert "1" edges to weight=1, and 0 means "no edge".
    # dijkstra expects a weighted adjacency; zeros are treated as
    # "no edge" except diagonal. So we keep as 0/1 and ask for
    # unweighted=True.
    dist, predecessors = dijkstra(
        A_csr,
        directed=False,
        unweighted=True,
        return_predecessors=True
    )

    # dist is (num_nodes, num_nodes): hop distances
    # predecessors is (num_nodes, num_nodes): predecessor indices

    # ---------------------------
    # Build route commands for this epoch
    # ---------------------------

    ## IP route handling
    route_string: Dict[int, str] = {}   # keys are src_idx (int), values are route cmd strings
    for target_node in node_to_route:
        if target_node not in node_map:
            log.warning(f"\t ⚠️ Node '{target_node}' not found in configuration, skipping routing.")
            continue
        target_idx = node_map[target_node]
        for src_idx in range(num_nodes):
            if src_idx == target_idx:
                continue  # skip self

            next_hops = pick_primary_secondary_next_hops(A_csr, dist, src_idx, target_idx)
            if next_hops == previous_next_hops.get(src_idx, {}).get(target_idx, []):
                continue  # no change in next hops, skip
            previous_next_hops.setdefault(src_idx, {})[target_idx] = next_hops
            if not next_hops:
                log.warning(f"\t ⚠️ No path from {inv_node_map[src_idx]} to {target_node}, skipping.")
                continue

            dst_ip = ip_map.get(target_node, "UNKNOWN")
            if dst_ip == "UNKNOWN":
                log.warning(f"\t ⚠️ No IP found for target node '{target_node}', skipping route entry.")
                continue

            if src_idx not in route_string:
                route_string[src_idx] = "sleep 0.1"   # small delay to allow the possible preeceding setup of the interface

            # Build commands
            def mk_cmd(nh_idx: int, metric: int) -> str:
                nh_name = inv_node_map[nh_idx]
                nh_ip = ip_map.get(nh_name, "UNKNOWN")
                if nh_ip == "UNKNOWN":
                    return ""
                dev_name = f"vl_{nh_name}_1"
                return f"ip route replace {dst_ip} via {nh_ip} dev {dev_name} metric {metric} onlink"

            # Primary (lowest metric)
            cmd1 = mk_cmd(next_hops[0], metric=100)
            if not cmd1:
                log.warning(f"\t ⚠️ Missing IP for primary next hop from {inv_node_map[src_idx]} to {target_node}, skipping.")
                continue
            route_string[src_idx] = route_string[src_idx] + "; " + cmd1

            # Secondary (higher metric, different next hop)
            if len(next_hops) == 2:
                cmd2 = mk_cmd(next_hops[1], metric=200)
                if cmd2:
                    route_string[src_idx] = route_string[src_idx] + "; " + cmd2
    
    # Print out the route_string for this epoch
    new_epoch_data = {}
    new_epoch_data["time"] = epoch_data.get("time", "")
    ## reduce time (utc) of new epoch by offset_seconds seconds
    try:
        t = datetime.fromisoformat(new_epoch_data["time"].replace("Z", "+00:00"))
        t_new = t - timedelta(seconds=offset_seconds)
        new_epoch_data["time"] = t_new.strftime("%Y-%m-%dT%H:%M:%SZ")
        new_epoch_data["run"] = {}
        for src_idx, routes in route_string.items():
            src_name = inv_node_map[src_idx]
            run = new_epoch_data.get("run", {}).get(src_name, [])
            # add at the start of run array the route commands
            run.append(routes)
            new_epoch_data["run"][src_name] = run
    except ValueError as ve:
        log.warning(f"\t ⚠️ Error parsing time '{new_epoch_data['time']}': {ve}")
    return new_epoch_data

def compute_routes(etcd_client, 
                   epoch_dir: str, 
                   file_pattern: str, 
                   out_epoch_dir: str, 
                   node_to_route: list, 
                   node_type_to_route: list, 
                   drain_before_break_offset: int,
                   link_creation_offset: int
                   ) -> None:

    try:
        # Load configuration and build node_map (same logic you already have)
        log.info("📁 Loading configuration from etcd...")
        nodes = get_prefix_data(etcd_client, "/config/nodes")
        log.info(f"🔎 Found {len(nodes)} nodes in configuration.")
        log.info(f"ℹ️ Node type to route: {node_type_to_route}")

        node_map: dict[str, int] = {}
        idx = 0
        node_to_route = []
        for name, node_info in nodes.items():
            node_map[name] = idx; idx += 1
            if node_info.get("type") in node_type_to_route or "any" in node_type_to_route:
                node_to_route.append(name)
        
        ip_map: dict[str, str] = {}
            ## Configure /etc/hosts entries for all known satellites/grounds/users
        prefix = "/config/etchosts/"
        for value, meta in etcd_client.get_prefix(prefix):
            node_name = meta.key.decode().split('/')[-1]
            ip_addr = value.decode().strip()
            if ip_addr:
                ip_map[node_name] = ip_addr

        inv_node_map = {i: name for name, i in node_map.items()}
        num_nodes = len(node_map)
        if num_nodes == 0:
            raise ValueError("Configuration has 0 nodes.")
        
        log.info(f"🛣️ Computing routes ...")    
        # ---------------------------
        # A as a sparse adjacency matrix
        # - Use LIL while mutating (fast incremental set)
        # - Convert to CSR when running graph algorithms (fast traversal)
        # ---------------------------
        A_lil = lil_matrix((num_nodes, num_nodes), dtype="uint8")  # 0/1 adjacency
        unnumbered_file_pattern = file_pattern.replace("*", "??????")
        epoch_files = list_epoch_files(epoch_dir, file_pattern)
        log.info(f"\t 🔎 Found {len(epoch_files)} epoch files to process.")
        if not epoch_files:
            raise FileNotFoundError(f"\t No epoch files found in '{epoch_dir}' with pattern '{file_pattern}'")

        # Basic stats
        num_epochs = 0
        previous_next_hops: Dict[int,Dict[int,list]] = {} # keys are src_idx (int) and target_node (int), values are next_hops tupla
        file_counter = 1
        for path in epoch_files:
            with open(path, "r", encoding="utf-8") as f:
                epoch_data = json.load(f)
            log.info(f"\t 💾 Processing epoch file: {path} (time: {epoch_data.get('time','UNKNOWN')})")
            if drain_before_break_offset > 0:
                # Create drain-before-break epoch first
                dbb_epoch_data = compute_routes_single_epoch(
                    epoch_data = epoch_data,
                    node_map = node_map,
                    A_lil = A_lil,
                    node_to_route = node_to_route,
                    previous_next_hops = previous_next_hops,
                    drain_before_break = True,
                    offset_seconds = drain_before_break_offset,
                    num_nodes = num_nodes,
                    inv_node_map = inv_node_map,
                    ip_map = ip_map
                )
                if dbb_epoch_data['run'] != {}:
                    file_path = unnumbered_file_pattern.replace("??????", f"{file_counter}")
                    file_counter+=1
                    out_epoch_path = os.path.join(out_epoch_dir, os.path.basename(file_path))
                        # Write out new epoch file with routes added
                    with open(out_epoch_path, "w", encoding="utf-8") as f_out:
                        json.dump(dbb_epoch_data, f_out, indent=2)
            
            # Add original epoch
            file_path = unnumbered_file_pattern.replace("??????", f"{file_counter}")
            file_counter+=1
            out_epoch_path = os.path.join(out_epoch_dir, os.path.basename(file_path))
            with open(out_epoch_path, "w", encoding="utf-8") as f_out:
                json.dump(epoch_data, f_out, indent=2)
            
            # Add routes to original epoch
            new_epoch_data = compute_routes_single_epoch(
                epoch_data = epoch_data,
                node_map = node_map,
                A_lil = A_lil,
                node_to_route = node_to_route,
                previous_next_hops = previous_next_hops,
                drain_before_break = False,
                offset_seconds = -link_creation_offset,
                num_nodes = num_nodes,
                inv_node_map = inv_node_map,
                ip_map = ip_map
            )
            file_path = unnumbered_file_pattern.replace("??????", f"{file_counter}")
            file_counter+=1
            out_epoch_path = os.path.join(out_epoch_dir, os.path.basename(file_path))
            with open(out_epoch_path, "w", encoding="utf-8") as f_out:
                json.dump(new_epoch_data, f_out, indent=2)
            num_epochs += 1
            if num_epochs % 10 == 0:
                log.info(f"\t … processed {num_epochs}/{len(epoch_files)} epochs; ")            
        
        # For drain before break add additional epochs to support loop runs
        if drain_before_break_offset > 0:
            path = epoch_files[0] # first epoch
            with open(path, "r", encoding="utf-8") as f:
                epoch_data = json.load(f)
            dbb_epoch_data = compute_routes_single_epoch(
                epoch_data = epoch_data,
                node_map = node_map,
                A_lil = A_lil,
                node_to_route = node_to_route,
                previous_next_hops = previous_next_hops,
                drain_before_break = True,
                offset_seconds = drain_before_break_offset,
                num_nodes = num_nodes,
                inv_node_map = inv_node_map,
                ip_map = ip_map
            )
            file_counter = 0
            file_path = unnumbered_file_pattern.replace("??????", f"{file_counter}")
            out_epoch_path = os.path.join(out_epoch_dir, os.path.basename(file_path))
            # Write out new epoch file with routes added
            with open(out_epoch_path, "w", encoding="utf-8") as f_out:
                json.dump(dbb_epoch_data, f_out, indent=2)
    except Exception as e:
        raise e

# ==========================================
# MAIN
# ==========================================
def main() -> int:
    parser = argparse.ArgumentParser(description="Manage routing tables of nodes based on constellation epoch files.")

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
        "--etcd-ca-cert",
        default=os.getenv("ETCD_CA_CERT", None ),
        help="Path to Etcd CA certificate (default: env ETCD_CA_CERT or None)",
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
        "--out-epoch-dir",
        default="constellation-epochs-routes",
        help="Output directory for processed epochs with routing info (default: constellation-epochs-routes).",
    )
    parser.add_argument(
        "--node-to-route",
        default="",
        help="Comma-separated list of nodes to route (default: empty).",
    )
    parser.add_argument(
        "--node-type-to-route",
        default="any",
        help="Comma-separated list of node types to route [e.g., satellite, gateway, user, any] (default: any).",
    )
    parser.add_argument(
        "--drain-before-break-offset",
        type=int,
        default=0,
        help="Offset in seconds for drain-before-break route replacement (default: 0, no drain before break).",
    )
    parser.add_argument(
        "--link-creation-offset",
        type=int,
        default=2,
        help="Offset in seconds for route replacement after link creation (default: 2).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (default: INFO)",
    )

    args = parser.parse_args()
    log.setLevel(args.log_level.upper())

    etcd_client = etcd3.client(
        host=args.etcd_host,
        port=args.etcd_port,
        user=args.etcd_user,
        password=args.etcd_password,
        ca_cert=args.etcd_ca_cert,
    )  
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
        epoch_dir_etcd, file_pattern_etcd = load_epoch_dir_and_pattern_from_etcd(etcd_client)
        epoch_dir = epoch_dir or epoch_dir_etcd
        file_pattern = file_pattern or file_pattern_etcd
    
    # Ensure output directory exists
    if not os.path.exists(args.out_epoch_dir):
        os.makedirs(args.out_epoch_dir)
    # Check if output directory is empty
    if os.listdir(args.out_epoch_dir):
        # not empty, ask if user wants to clean before to preceed (y/n with y as default)
        response = input(f"⚠️ Output directory '{args.out_epoch_dir}' is not empty. Do you want to clear it before proceeding? (y/n) [y]: ")
        if response.lower() in ["", "y", "yes"]:
            for filename in os.listdir(args.out_epoch_dir):
                file_path = os.path.join(args.out_epoch_dir, filename)
                try:
                    if os.path.isfile(file_path):
                        os.unlink(file_path)
                except Exception as e:
                    log.error(f"❌ Could not delete file '{file_path}': {e}")
                    return 4
        else:
            log.error("❌ Please specify an empty output directory to proceed.")    
            return 3
        
    try:
        compute_routes(
            etcd_client = etcd_client,
            epoch_dir = epoch_dir,
            file_pattern = file_pattern,
            out_epoch_dir=args.out_epoch_dir,
            node_to_route = args.node_to_route.split(','),
            node_type_to_route = args.node_type_to_route.split(','),
            drain_before_break_offset = args.drain_before_break_offset,
            link_creation_offset = args.link_creation_offset
        )
    except Exception as e:
        log.error(f"❌ Error during route computation: {e}")
        return 1
    log.info("👍 Route computation completed.")
    log.info(f"👉 Processed epochs with routing info are in directory: {args.out_epoch_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())