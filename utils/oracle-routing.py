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
from scipy.sparse import lil_matrix, csr_matrix
# (optional, but recommended for dijkstra)
from scipy.sparse.csgraph import dijkstra

# ==========================================
# ETCD CONNECTION
# ==========================================
def connect_etcd(etcd_host: str, etcd_port: int, etcd_user = None, etcd_password = None, etcd_ca_cert = None):
    try:
        print(f"📁 Connecting to Etcd at {etcd_host}:{etcd_port}...")
        if etcd_user and etcd_password:
            return etcd3.client(host=etcd_host, port=etcd_port, user=etcd_user, password=etcd_password, ca_cert=etcd_ca_cert)
        else:
            return etcd3.client(host=etcd_host, port=etcd_port)
    except Exception as e:
        print(f"❌ Failed to initialize Etcd client: {e}")
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
        print(f"⚠️ Failed to load epoch configuration from Etcd, using defaults. Details: {e}")
        return default_dir, default_pattern
    
def get_prefix_data(etcd, prefix: str) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for value, metadata in etcd.get_prefix(prefix):
        key = metadata.key.decode('utf-8').split('/')[-1]
        try:
            data[key] = json.loads(value.decode('utf-8'))
        except json.JSONDecodeError:
            print(f"⚠️ Warning: Could not parse JSON for key {key} under {prefix}")
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
                   make_before_break_delay: int,
                   num_nodes: int,
                   inv_node_map: dict,
                   ip_map: dict
                   ) -> dict:
    
    # Apply link-add only if is not a make-before-break epoch
    if make_before_break_delay == 0 :
        print(f"⏭️ Skipping link-adds for make-before-break epoch at {epoch_data.get('time', '')}")
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
    route_string: Dict[int, list] = {}   # keys are src_idx (int), values are route cmd strings
    for target_node in node_to_route:
        if target_node not in node_map:
            print(f"⚠️ Node '{target_node}' not found in configuration, skipping routing.")
            continue
        target_idx = node_map[target_node]
        for src_idx in range(num_nodes):
            if src_idx == target_idx:
                continue  # skip self

            next_hops = pick_primary_secondary_next_hops(A_csr, dist, src_idx, target_idx)
            if next_hops == previous_next_hops.get(src_idx, {}).get(target_idx, []):
                print(f"ℹ️ No change in next hops from {inv_node_map[src_idx]} to {target_node}, skipping.")
                continue  # no change in next hops, skip
            previous_next_hops.setdefault(src_idx, {})[target_idx] = next_hops
            if not next_hops:
                print(f"⚠️ No path from {inv_node_map[src_idx]} to {target_node}, skipping.")
                continue

            dst_ip = ip_map.get(target_node, "UNKNOWN")
            if dst_ip == "UNKNOWN":
                print(f"⚠️ No IP found for target node '{target_node}', skipping route entry.")
                continue

            if src_idx not in route_string:
                route_string[src_idx] = ["sleep 0.1"]   # small delay to allow the possible preeceding setup of the interface

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
                print(f"⚠️ Missing IP for primary next hop from {inv_node_map[src_idx]} to {target_node}, skipping.")
                continue
            route_string[src_idx].append(cmd1)

            # Secondary (higher metric, different next hop)
            if len(next_hops) == 2:
                cmd2 = mk_cmd(next_hops[1], metric=200)
                if cmd2:
                    route_string[src_idx].append(cmd2)
    
    # Print out the route_string for this epoch
    if make_before_break_delay == 0:
        new_epoch_data = epoch_data.copy()
        for src_idx, routes in route_string.items():
            src_name = inv_node_map[src_idx]
            run = new_epoch_data.get("run", {}).get(src_name, [])
            # add at the start of run array the route commands
            for route in routes:
                run.append(route)
            new_epoch_data["run"][src_name] = run
    else:
        new_epoch_data = {}
        new_epoch_data["time"] = epoch_data.get("time", "")
        ## reduce time (utc) of mbb epoch by make_before_break_delay seconds
        try:
            t = datetime.fromisoformat(new_epoch_data["time"].replace("Z", "+00:00"))
            t_new = t - timedelta(milliseconds=make_before_break_delay)
            new_epoch_data["time"] = t_new.strftime("%Y-%m-%dT%H:%M:%SZ")
            new_epoch_data["run"] = {}
            for src_idx, routes in route_string.items():
                src_name = inv_node_map[src_idx]
                run = new_epoch_data.get("run", {}).get(src_name, [])
                # add at the start of run array the route commands
                for route in routes:
                    run.append(route)
                new_epoch_data["run"][src_name] = run
        except ValueError as ve:
            print(f"⚠️ Could not parse time '{new_epoch_data['time']}' for make-before-break epoch adjustment: {ve}")
    return new_epoch_data

def compute_routes(etcd_client, 
                   epoch_dir: str, 
                   file_pattern: str, 
                   out_epoch_dir: str, 
                   node_to_route: list, 
                   node_class_to_route: list, 
                   make_before_break_delay: int
                   ) -> None:

    # Load configuration and build node_map (same logic you already have)
    print("📁 Loading configuration from etcd...")
    satellites = get_prefix_data(etcd_client, "/config/satellites")
    if not satellites:
        raise ValueError("No /config/satellites found in Etcd.")
    users = get_prefix_data(etcd_client, "/config/users")
    if not users:
        raise ValueError("No /config/users found in Etcd.")
    grounds = get_prefix_data(etcd_client, "/config/grounds")
    if not grounds:
        raise ValueError("No /config/grounds found in Etcd.")
    print(f"🔎 Found {len(satellites)} satellites, {len(users)} users, {len(grounds)} ground stations in configuration.")

    node_map: dict[str, int] = {}
    idx = 0
    for name in satellites.keys():
        node_map[name] = idx; idx += 1
    for name in users.keys():
        node_map[name] = idx; idx += 1
    for name in grounds.keys():
        node_map[name] = idx; idx += 1
    
    ip_map: dict[str, str] = {}
        ## Configure /etc/hosts entries for all known satellites/grounds/users
    print("📝 Loading /etchosts from etcd for satellites, grounds and users IP addressing...")
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
    
    # build node_to_route as set by adding all nodes of the node_to_route classes
    print("Node class to route:", node_class_to_route)
    if node_class_to_route:
        if "satellites" in node_class_to_route or "all" in node_class_to_route:
            for name in satellites.keys():
                if name not in node_to_route:
                    node_to_route.append(name)
        if "users" in node_class_to_route or "all" in node_class_to_route:
            for name in users.keys():
                if name not in node_to_route:
                    node_to_route.append(name)
        if "grounds" in node_class_to_route or "all" in node_class_to_route:
            for name in grounds.keys():
                if name not in node_to_route:
                    node_to_route.append(name)
    # remove empty strings
    node_to_route = [n for n in node_to_route if n]
    # remove duplicates
    node_to_route = list(set(node_to_route))
    print(f"🛣️ Computing routes to the following nodes: {node_to_route}")
          
    # ---------------------------
    # A as a sparse adjacency matrix
    # - Use LIL while mutating (fast incremental set)
    # - Convert to CSR when running graph algorithms (fast traversal)
    # ---------------------------
    A_lil = lil_matrix((num_nodes, num_nodes), dtype="uint8")  # 0/1 adjacency
    unnumbered_file_pattern = file_pattern.replace("*", "??????")
    epoch_files = list_epoch_files(epoch_dir, file_pattern)
    print(f"🔎 Found {len(epoch_files)} epoch files to process.")
    if not epoch_files:
        raise FileNotFoundError(f"No epoch files found in '{epoch_dir}' with pattern '{file_pattern}'")

    # Basic stats
    num_epochs = 0

    previous_next_hops: Dict[int,Dict[int,list]] = {} # keys are src_idx (int) and target_node (int), values are next_hops tupla
    file_counter = 0
    for path in epoch_files:
        with open(path, "r", encoding="utf-8") as f:
            epoch_data = json.load(f)

        if make_before_break_delay > 0:
            mbb_epoch_data = compute_routes_single_epoch(
                epoch_data = epoch_data,
                node_map = node_map,
                A_lil = A_lil,
                node_to_route = node_to_route,
                previous_next_hops = previous_next_hops,
                make_before_break_delay = make_before_break_delay,
                num_nodes = num_nodes,
                inv_node_map = inv_node_map,
                ip_map = ip_map
            )
            if not os.path.exists(out_epoch_dir):
                os.makedirs(out_epoch_dir)
            file_path = unnumbered_file_pattern.replace("??????", f"{file_counter}")
            file_counter+=1
            out_epoch_path = os.path.join(out_epoch_dir, os.path.basename(file_path))
            print("💾 Writing make before break epoch file with routes...")
                # Write out new epoch file with routes added
            with open(out_epoch_path, "w", encoding="utf-8") as f_out:
                json.dump(mbb_epoch_data, f_out, indent=2)
        
        new_epoch_data = compute_routes_single_epoch(
            epoch_data = epoch_data,
            node_map = node_map,
            A_lil = A_lil,
            node_to_route = node_to_route,
            previous_next_hops = previous_next_hops,
            make_before_break_delay = 0,
            num_nodes = num_nodes,
            inv_node_map = inv_node_map,
            ip_map = ip_map
        )
        if not os.path.exists(out_epoch_dir):
            os.makedirs(out_epoch_dir)
        file_path = unnumbered_file_pattern.replace("??????", f"{file_counter}")
        file_counter+=1
        out_epoch_path = os.path.join(out_epoch_dir, os.path.basename(file_path))
        print("💾 Writing updated epoch file with routes...")
        with open(out_epoch_path, "w", encoding="utf-8") as f_out:
            json.dump(new_epoch_data, f_out, indent=2)
        num_epochs += 1
        if num_epochs % 10 == 0:
            print(f"… processed {num_epochs}/{len(epoch_files)} epochs; ")            


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
        "--node-class-to-route",
        default="all",
        help="Comma-separated list of node classes to route [satellites, grounds, users, all] (default: all).",
    )
    parser.add_argument(
        "--make-before-break-delay",
        type=int,
        default=0,
        help="Delay in milliseconds for make-before-break routing (default: 0).",
    )

    args = parser.parse_args()

    etcd_client = etcd3.client(host=args.etcd_host, port=args.etcd_port)
    try:
        etcd_client.status()
    except Exception as e:
        print(f"❌ Could not connect to Etcd at {args.etcd_host}:{args.etcd_port}. Is it running?")
        print(f"Details: {e}")
        return 2

    # If an epoch-config file is provided, load it and use it unless user overrides epoch-dir/pattern explicitly.
    epoch_dir = args.epoch_dir
    file_pattern = args.file_pattern
    if epoch_dir is None or file_pattern is None:
        epoch_dir_etcd, file_pattern_etcd = load_epoch_dir_and_pattern_from_etcd(etcd_client)
        epoch_dir = epoch_dir or epoch_dir_etcd
        file_pattern = file_pattern or file_pattern_etcd
      
    compute_routes(
        etcd_client = etcd_client,
        epoch_dir = epoch_dir,
        file_pattern = file_pattern,
        out_epoch_dir=args.out_epoch_dir,
        node_to_route = args.node_to_route.split(','),
        node_class_to_route = args.node_class_to_route.split(','),
        make_before_break_delay = args.make_before_break_delay
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())