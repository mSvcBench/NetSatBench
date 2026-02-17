#!/usr/bin/env python3
"""
oracle-routing-ipv6.py

IPv6-capable variant of oracle-routing.py:
- Builds a (sparse) adjacency matrix from epoch files (links-add/links-del)
- Computes primary/secondary next hops (hop-count shortest paths)
- Emits *new* epoch JSONs that inject route commands using:
    - IPv4: ip route replace ...
    - IPv6: ip -6 route replace .../128 ...

This mirrors the structure/behavior of the original oracle-routing.py,
but adds:
  * --ip-version {4,6}
  * --etcd-etchosts-prefix /config/etchosts-ipv6/ (override)
  * IPv6 prefix-length handling (default /128 for host routes)

Assumptions (same as original):
- Node-to-node link devices are named like: vl_<NEIGHBORNAME>_1
- Etcd stores node IPs under a prefix that maps node_name -> ip string.
"""

import calendar
import time
from typing import Any, Dict, List, Tuple
import os
import re
import json
import argparse
from glob import glob
from datetime import datetime, timedelta
import sys
import logging

import etcd3
from scipy.sparse import lil_matrix, csr_matrix
from scipy.sparse.csgraph import dijkstra

logging.basicConfig(level="INFO", format="[%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ==========================================
# HELPERS
# ==========================================
def last_numeric_suffix(path: str) -> int:
    basename = os.path.basename(path)
    matches = re.findall(r"(\d+)", basename)
    return int(matches[-1]) if matches else -1

def list_epoch_files(epoch_dir: str, file_pattern: str) -> List[str]:
    if not epoch_dir or not file_pattern:
        return []
    search_path = os.path.join(epoch_dir, file_pattern)
    return sorted(glob(search_path), key=last_numeric_suffix)

def load_epoch_dir_and_pattern_from_etcd(etcd_client) -> Tuple[str, str]:
    default_dir = "epochs"
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

def is_ipv6(addr: str) -> bool:
    return ":" in addr

# ==========================================
# ROUTE COMPUTATION LOGIC
# ==========================================
def pick_primary_secondary_next_hops(A_csr: csr_matrix, dist, src_idx: int, target_idx: int) -> list[int]:
    """
    Returns [primary_nh] or [primary_nh, secondary_nh].
    Primary = shortest path (hop count).
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
        cands.append((1 + d_nt, n))  # cost constrained to start with src->n

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
    *,
    epoch_data: dict,
    node_map: dict,
    A_lil: lil_matrix,
    node_to_route: list,
    previous_next_hops: Dict[int, Dict[int, list]],
    drain_before_break: bool,
    offset_seconds: int,
    num_nodes: int,
    inv_node_map: dict,
    ip_map: dict,
    ip_version: int
) -> dict:
    # Apply link-add only if not drain-before-break epoch
    if not drain_before_break:
        for link_add in epoch_data.get("links-add", []):
            src = link_add.get("endpoint1")
            dst = link_add.get("endpoint2")
            if src not in node_map or dst not in node_map:
                continue
            i = node_map[src]
            j = node_map[dst]
            if i == j:
                continue
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
        if A_lil[i, j] != 0:
            A_lil[i, j] = 0
            A_lil[j, i] = 0

    # Run Dijkstra (unweighted hop-count)
    A_csr: csr_matrix = A_lil.tocsr()
    dist, _predecessors = dijkstra(A_csr, directed=False, unweighted=True, return_predecessors=True)

    # Build route commands for this epoch
    route_string: Dict[int, str] = {}   # src_idx -> cmd string
    
    for target_node in node_to_route:
        if target_node not in node_map:
            log.warning(f"\t ⚠️ Node '{target_node}' not found in configuration, skipping routing.")
            continue
        target_idx = node_map[target_node]

        dst_ip = ip_map.get(target_node, "UNKNOWN")
        if dst_ip == "UNKNOWN":
            log.warning(f"\t ⚠️ No IP found for target node '{target_node}', skipping route entry.")
            continue

        # Basic safety: enforce ip_version selection
        if ip_version == 6 and not is_ipv6(dst_ip):
            log.warning(f"\t ⚠️ Target '{target_node}' has non-IPv6 IP '{dst_ip}' but --ip-version=6. Skipping.")
            continue
        if ip_version == 4 and is_ipv6(dst_ip):
            log.warning(f"\t ⚠️ Target '{target_node}' has IPv6 IP '{dst_ip}' but --ip-version=4. Skipping.")
            continue
        
        for src_idx in range(num_nodes):
            if src_idx == target_idx:
                continue

            next_hops = pick_primary_secondary_next_hops(A_csr, dist, src_idx, target_idx)
            if next_hops == previous_next_hops.get(src_idx, {}).get(target_idx, []):
                continue
            previous_next_hops.setdefault(src_idx, {})[target_idx] = next_hops

            if not next_hops:
                log.warning(f"\t ⚠️ No path from {inv_node_map[src_idx]} to {target_node}, skipping.")
                continue

            if src_idx not in route_string:
                route_string[src_idx] = "sleep 0.1"  # allow preceding interface setup

            def mk_cmd(nh_idx: int, metric: int) -> str:
                nh_name = inv_node_map[nh_idx]
                nh_ip = ip_map.get(nh_name, "UNKNOWN")
                if nh_ip == "UNKNOWN":
                    return ""
                if ip_version == 6 and not is_ipv6(nh_ip):
                    return ""
                if ip_version == 4 and is_ipv6(nh_ip):
                    return ""

                dev_name = f"vl_{nh_name}_1"
                
                if ip_version == 6:
                    return f"extra/routing/add_ipv6_route_ll.sh {dev_name} {dst_ip} {metric}"
                return f"ip route replace {dst_ip} via {nh_ip} dev {dev_name} metric {metric} onlink"

            nh_idx = next_hops[0]
            cmd1 = mk_cmd(nh_idx, metric=100)
            if not cmd1:
                log.warning(f"\t ⚠️ Missing/invalid IP for primary next hop from {inv_node_map[src_idx]} to {target_node}, skipping.")
                continue
            route_string[src_idx] =route_string.get(src_idx, "") + "; " + cmd1

            if len(next_hops) == 2:
                cmd2 = mk_cmd(next_hops[1], metric=200)
                if cmd2:
                    route_string[src_idx] = route_string.get(src_idx, "") + "; " + cmd2

    # Create new epoch payload (same structure as original)
    new_epoch_data: Dict[str, Any] = {}
    new_epoch_data["time"] = epoch_data.get("time", "")

    try:
        t = datetime.fromisoformat(new_epoch_data["time"].replace("Z", "+00:00"))
        t_new = t - timedelta(seconds=offset_seconds)
        new_epoch_data["time"] = t_new.strftime("%Y-%m-%dT%H:%M:%SZ")
        new_epoch_data["run"] = {}

        for src_idx, routes in route_string.items():
            src_name = inv_node_map[src_idx]
            run = new_epoch_data.get("run", {}).get(src_name, [])
            run.append(routes)
            new_epoch_data["run"][src_name] = run

    except ValueError as ve:
        log.warning(f"\t ⚠️ Error parsing time '{new_epoch_data['time']}': {ve}")

    return new_epoch_data

def compute_routes(
    *,
    etcd_client,
    epoch_dir: str,
    file_pattern: str,
    out_epoch_dir: str,
    node_type_to_route: list,
    drain_before_break_offset: int,
    link_creation_offset: int,
    ip_version: int,
    etcd_etchosts_prefix: str
) -> None:
    # Load config and build node map (same as original)
    log.info("📁 Loading configuration from etcd...")
    nodes = get_prefix_data(etcd_client, "/config/nodes")
    log.info(f"🔎 Found {len(nodes)} nodes in configuration.")
    log.info(f"ℹ️ Node type to route: {node_type_to_route}")

    node_map: Dict[str, int] = {}
    idx = 0
    node_to_route: List[str] = []
    for name, node_info in nodes.items():
        node_map[name] = idx
        idx += 1
        if node_info.get("type") in node_type_to_route or "any" in node_type_to_route:
            node_to_route.append(name)
            # log.info(f"\t 🚦 Will route to node '{name}' of type '{node_info.get('type')}'")

    # Load IP map from etcd prefix
    ip_map: Dict[str, str] = {}
    for value, meta in etcd_client.get_prefix(etcd_etchosts_prefix):
        node_name = meta.key.decode().split('/')[-1]
        ip_addr = value.decode().strip()
        if ip_addr:
            ip_map[node_name] = ip_addr
            found_any = True

    if not found_any:
        log.warning(f"⚠️ No IPs found under prefixes {etcd_etchosts_prefix}. Routes will be skipped due to missing IPs.")

    inv_node_map = {i: name for name, i in node_map.items()}
    num_nodes = len(node_map)
    if num_nodes == 0:
        raise ValueError("⚠️ Configuration has 0 nodes.")

    log.info("🛣️ Computing routes ...")
    A_lil = lil_matrix((num_nodes, num_nodes), dtype="uint8")
    unnumbered_file_pattern = file_pattern.replace("*", "??????")
    epoch_files = list_epoch_files(epoch_dir, file_pattern)

    log.info(f"\t 🔎 Found {len(epoch_files)} epoch files to process.")
    if not epoch_files:
        raise FileNotFoundError(f"\t No epoch files found in '{epoch_dir}' with pattern '{file_pattern}'")

    num_epochs = 0
    previous_next_hops: Dict[int, Dict[int, list]] = {}
    file_counter = 1

    for path in epoch_files:
        with open(path, "r", encoding="utf-8") as f:
            epoch_data = json.load(f)

        log.info(f"\t 💾 Processing epoch file: {path} (time: {epoch_data.get('time','UNKNOWN')})")

        # 1) Optional drain-before-break epoch first
        if drain_before_break_offset > 0:
            dbb_epoch_data = compute_routes_single_epoch(
                epoch_data=epoch_data,
                node_map=node_map,
                A_lil=A_lil,
                node_to_route=node_to_route,
                previous_next_hops=previous_next_hops,
                drain_before_break=True,
                offset_seconds=drain_before_break_offset,
                num_nodes=num_nodes,
                inv_node_map=inv_node_map,
                ip_map=ip_map,
                ip_version=ip_version
            )
            if dbb_epoch_data.get("run", {}) != {}:
                file_path = unnumbered_file_pattern.replace("??????", f"{file_counter}")
                file_counter += 1
                out_epoch_path = os.path.join(out_epoch_dir, os.path.basename(file_path))
                with open(out_epoch_path, "w", encoding="utf-8") as f_out:
                    json.dump(dbb_epoch_data, f_out, indent=2)

        # 2) Add original epoch
        file_path = unnumbered_file_pattern.replace("??????", f"{file_counter}")
        file_counter += 1
        out_epoch_path = os.path.join(out_epoch_dir, os.path.basename(file_path))
        with open(out_epoch_path, "w", encoding="utf-8") as f_out:
            json.dump(epoch_data, f_out, indent=2)

        # 3) Add routes to original epoch (shifted earlier by link_creation_offset)
        new_epoch_data = compute_routes_single_epoch(
            epoch_data=epoch_data,
            node_map=node_map,
            A_lil=A_lil,
            node_to_route=node_to_route,
            previous_next_hops=previous_next_hops,
            drain_before_break=False,
            offset_seconds=-link_creation_offset,
            num_nodes=num_nodes,
            inv_node_map=inv_node_map,
            ip_map=ip_map,
            ip_version=ip_version,
        )

        file_path = unnumbered_file_pattern.replace("??????", f"{file_counter}")
        file_counter += 1
        out_epoch_path = os.path.join(out_epoch_dir, os.path.basename(file_path))
        with open(out_epoch_path, "w", encoding="utf-8") as f_out:
            json.dump(new_epoch_data, f_out, indent=2)

        num_epochs += 1
        if num_epochs % 10 == 0:
            log.info(f"\t … processed {num_epochs}/{len(epoch_files)} epochs;")

    # For drain-before-break: add additional epoch at file_counter=0 (same as original)
    if drain_before_break_offset > 0:
        path0 = epoch_files[0]
        with open(path0, "r", encoding="utf-8") as f:
            epoch_data0 = json.load(f)
        dbb_epoch_data0 = compute_routes_single_epoch(
            epoch_data=epoch_data0,
            node_map=node_map,
            A_lil=A_lil,
            node_to_route=node_to_route,
            previous_next_hops=previous_next_hops,
            drain_before_break=True,
            offset_seconds=drain_before_break_offset,
            num_nodes=num_nodes,
            inv_node_map=inv_node_map,
            ip_map=ip_map,
            ip_version=ip_version
        )
        file_path0 = unnumbered_file_pattern.replace("??????", "0")
        out_epoch_path0 = os.path.join(out_epoch_dir, os.path.basename(file_path0))
        with open(out_epoch_path0, "w", encoding="utf-8") as f_out:
            json.dump(dbb_epoch_data0, f_out, indent=2)

# ==========================================
# MAIN
# ==========================================
def main() -> int:
    parser = argparse.ArgumentParser(description="Compute routes from epoch files and emit route-injected epoch JSONs (IPv4/IPv6).")

    parser.add_argument("--etcd-host", default=os.getenv("ETCD_HOST", "127.0.0.1"), help="Etcd host (default: env ETCD_HOST or 127.0.0.1)")
    parser.add_argument("--etcd-port", type=int, default=int(os.getenv("ETCD_PORT", 2379)), help="Etcd port (default: env ETCD_PORT or 2379)")
    parser.add_argument("--etcd-user", default=os.getenv("ETCD_USER", None), help="Etcd user (default: env ETCD_USER or None)")
    parser.add_argument("--etcd-password", default=os.getenv("ETCD_PASSWORD", None), help="Etcd password (default: env ETCD_PASSWORD or None)")
    parser.add_argument("--etcd-ca-cert", default=os.getenv("ETCD_CA_CERT", None), help="Etcd CA certificate (default: env ETCD_CA_CERT or None)")

    parser.add_argument("--epoch-dir", help="Epoch directory, takes precedence over Etcd.")
    parser.add_argument("--file-pattern", help="Epoch filename pattern, takes precedence over Etcd.")
    parser.add_argument("--out-epoch-dir", help="Output dir for processed epochs with route injection.")

    parser.add_argument("--node-type-to-route", default="any", help="Comma-separated node types to route to (default: any). Matches against node 'type' in config. Use 'any' to route to all nodes.")

    parser.add_argument("--drain-before-break-offset", type=int, default=0, help="Seconds offset for drain-before-break epoch. If 0, no separate drain-before-break epoch is emitted.")
    parser.add_argument("--link-creation-offset", type=int, default=1, help="Seconds offset for route replacement after link creation. Default: 1 second after link creation.")

    parser.add_argument("--ip-version", type=int, choices=[4, 6], default=4, help="Generate IPv4 or IPv6 route commands. Default: 4 (IPv4).")
    
    parser.add_argument("--log-level", default="INFO", help="Logging level (default: INFO).")

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

    # epoch dir / pattern resolution
    epoch_dir = args.epoch_dir
    file_pattern = args.file_pattern
    if epoch_dir is None or file_pattern is None:
        epoch_dir_etcd, file_pattern_etcd = load_epoch_dir_and_pattern_from_etcd(etcd_client)
        epoch_dir = epoch_dir or epoch_dir_etcd
        file_pattern = file_pattern or file_pattern_etcd

    # Ensure output directory exists / is empty
    if not os.path.exists(args.out_epoch_dir):
        os.makedirs(args.out_epoch_dir)

    if os.listdir(args.out_epoch_dir):
        response = input(f"⚠️ Output directory '{args.out_epoch_dir}' is not empty. Clear it before proceeding? (y/n) [y]: ")
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
            etcd_client=etcd_client,
            epoch_dir=epoch_dir,
            file_pattern=file_pattern,
            out_epoch_dir=args.out_epoch_dir,
            node_type_to_route=args.node_type_to_route.split(","),
            drain_before_break_offset=args.drain_before_break_offset,
            link_creation_offset=args.link_creation_offset,
            ip_version=args.ip_version,
            etcd_etchosts_prefix="/config/etchosts/" if args.ip_version == 4 else "/config/etchosts6/"
        )
    except Exception as e:
        log.error(f"❌ Error during route computation: {e}")
        return 1

    log.info("👍 Route computation completed.")
    log.info(f"👉 Processed epochs with routing info are in directory: {args.out_epoch_dir}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
