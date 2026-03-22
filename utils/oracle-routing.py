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
import math
import time
from typing import Any, Dict, List, Tuple
import os
import re
import json
import shutil
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
cross_type_penalty = 4096  # used to prefer next hop of the same type
link_delay_quantum_ms = 10 # used to limit routing flapping in delay mode 

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

def parse_epoch_time(epoch_time: str) -> datetime:
    return datetime.fromisoformat(epoch_time.replace("Z", "+00:00"))

def join_route_commands_with_sleep(
    commands: List[str],
    max_routes_per_epoch: int,
    sleep_seconds: int,
) -> str:
    if not commands:
        return ""
    if max_routes_per_epoch <= 0 or sleep_seconds <= 0:
        return "; ".join(commands)

    output_cmds: List[str] = []
    for idx, cmd in enumerate(commands):
        output_cmds.append(cmd)
        reached_batch_end = (idx + 1) % max_routes_per_epoch == 0
        is_last_cmd = idx == len(commands) - 1
        if reached_batch_end and not is_last_cmd:
            output_cmds.append(f"sleep {sleep_seconds}")
    return "; ".join(output_cmds)

def parse_delay(value) -> float:
    if not value: return 0.0
    val = str(value).strip()
    units = {
        # Normalize all delays to milliseconds.
        's': 1000.0, 'ms': 1.0, 'us': 0.001, 'ns': 0.000001}
    match = re.match(r"([0-9\.]+)([a-zA-Z]+)?", val)
    if not match: return 0.0
    try:
        num = float(match.group(1))
        unit = match.group(2)
        if unit and unit in units:
            return num * units[unit]
        #return num and delete unit
        return num
    except ValueError:
        return 0.0

# ==========================================
# ROUTE COMPUTATION LOGIC
# ==========================================
def pick_primary_secondary_next_hops(
    A_csr: csr_matrix,
    dist,
    src_idx: int,
    target_idx: int,
    previous_next_hops: list[int] | None = None,
) -> list[int]:
    """
    Returns [primary_nh] or [primary_nh, secondary_nh].
    Primary = lowest-cost path after applying small anti-flap penalties.
    Secondary = lowest-cost path among those whose first hop != primary.
    """
    d_st = dist[src_idx, target_idx]
    if d_st == float("inf") or src_idx == target_idx:
        return []

    previous_primary = previous_next_hops[0] if previous_next_hops else None
    previous_secondary = previous_next_hops[1] if previous_next_hops and len(previous_next_hops) > 1 else None

    # neighbors of src in CSR
    row_start = A_csr.indptr[src_idx]
    row_end = A_csr.indptr[src_idx + 1]
    neighbors = A_csr.indices[row_start:row_end]

    cands = []
    for n in neighbors:
        d_nt = dist[n, target_idx]
        if d_nt == float("inf"):
            continue
        anti_flap_penalty = 0.0
        if previous_primary is not None and n != previous_primary:
            anti_flap_penalty += 0.1
        if previous_secondary is not None and n != previous_secondary:
            anti_flap_penalty += 0.05
        cands.append((A_csr[src_idx, n] + d_nt + anti_flap_penalty, n))

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
    node_type: dict,
    A_lil: lil_matrix,
    node_to_route: list,
    node_to_install: list,
    previous_next_hops: Dict[int, Dict[int, list]],
    drain_before_break: bool,
    offset_seconds: int,
    num_nodes: int,
    inv_node_map: dict,
    ip_map: dict,
    ip_version: int,
    redundancy: bool,
    routing_metric: str,
    max_routes_per_epoch: int,
    sleep_seconds: int,
    route_change_count_by_node: Dict[str, int] | None = None,
) -> dict:
    
    def compute_link_weight(src: str, dst: str, link: dict) -> float:
        # Metric cost.
        if routing_metric == "hops":
            metric_cost = 1.0
        elif routing_metric == "delay":
            metric_cost = math.ceil(parse_delay(link.get("delay", 0)) / link_delay_quantum_ms)+1
        else:
            raise ValueError(f"Unsupported routing metric: {routing_metric}")

        # Cross-type bias (kept additive to metric cost).
        type_penalty = float(cross_type_penalty) if node_type.get(src) != node_type.get(dst) else 0.0
        return metric_cost + type_penalty
        
    no_links_added = 0
    no_links_updated = 0
    no_links_deleted = 0
    
    # Apply link-add and update only if not drain-before-break epoch
    if not drain_before_break:
        # Apply link add
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
                w = compute_link_weight(src, dst, link_add)
                A_lil[i, j] = w
                A_lil[j, i] = w
                no_links_added += 1
        
        # Apply link updatey
        if routing_metric != 'hops': 
            # no need to consider link updates if using hop count as metric, since weight is always 1 or cross_type_penalty
            for link_update in epoch_data.get("links-update", []):
                src = link_update.get("endpoint1")
                dst = link_update.get("endpoint2")
                if src not in node_map or dst not in node_map:
                    continue
                i = node_map[src]
                j = node_map[dst]
                if i == j:
                    continue
                w = compute_link_weight(src, dst, link_update)
                if A_lil[i, j] != w: # only update if weight changed and the change is above the delay tolerance threshold (to avoid flapping due to minor delay changes)
                    A_lil[i, j] = w
                    A_lil[j, i] = w
                    no_links_updated += 1

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
            no_links_deleted += 1


    if no_links_added == 0 and no_links_updated == 0 and no_links_deleted == 0:
        return {}
    
    # Run Dijkstra (unweighted hop-count)
    A_csr: csr_matrix = A_lil.tocsr()
    dist, _predecessors = dijkstra(A_csr, directed=False, unweighted=False, return_predecessors=True)

    # Build route commands for this epoch
    route_commands: Dict[str, List[str]] = {}   # src_name -> list of cmd strings

    def append_route_cmd(src_idx: int, cmd: str) -> None:
        src_name = inv_node_map[src_idx]
        route_commands.setdefault(src_name, []).append(cmd)
        if route_change_count_by_node is not None:
            route_change_count_by_node[src_name] = route_change_count_by_node.get(src_name, 0) + 1
    
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
        
        for src_node in node_to_install:
            if src_node not in node_map:
                log.warning(f"\t ⚠️ Node '{src_node}' not found in configuration, skipping route installation.")
                continue
            src_idx = node_map.get(src_node, None)
            if src_idx == target_idx:
                continue

            prior_next_hops = previous_next_hops.get(src_idx, {}).get(target_idx, [])
            next_hops = pick_primary_secondary_next_hops(
                A_csr,
                dist,
                src_idx,
                target_idx,
                previous_next_hops=prior_next_hops,
            )
            if next_hops == prior_next_hops:
                continue
            previous_next_hops.setdefault(src_idx, {})[target_idx] = next_hops

            if not next_hops:
                log.warning(f"\t ⚠️ No path from {inv_node_map[src_idx]} to {target_node}, skipping.")
                continue

            def route_add(src_name: str, dst_name: str, nh_name: str, metric: int) -> None:
                src_idx = node_map[src_name]

                nh_ip = ip_map.get(nh_name, "UNKNOWN")
                if nh_ip == "UNKNOWN":
                    raise ValueError(f"Missing IP for next hop node '{nh_name}' in Etcd under prefix.")
                if ip_version == 6 and not is_ipv6(nh_ip):
                   raise ValueError(f"Next hop '{nh_name}' has non-IPv6 IP '{nh_ip}' but --ip-version=6.")
                if ip_version == 4 and is_ipv6(nh_ip):
                    raise ValueError(f"Next hop '{nh_name}' has IPv6 IP '{nh_ip}' but --ip-version=4.")
                
                dst_ip = ip_map.get(dst_name, "UNKNOWN")
                if dst_ip == "UNKNOWN":
                    raise ValueError(f"Missing IP for target node '{dst_name}' in Etcd under prefix.")
                if ip_version == 6 and not is_ipv6(dst_ip):
                    raise ValueError(f"Target '{dst_name}' has non-IPv6 IP '{dst_ip}' but --ip-version=6.")
                if ip_version == 4 and is_ipv6(dst_ip):
                    raise ValueError(f"Target '{dst_name}' has IPv6 IP '{dst_ip}' but --ip-version=4.")
               
                dev_name = f"vl_{nh_name}_1"

                if ip_version == 6:
                    route_append_str = f"extra/routing/add_ipv6_route_ll.sh {dev_name} {dst_ip} {metric}"
                    append_route_cmd(src_idx, route_append_str)
                    return 
                elif ip_version == 4:
                    route_append_str = f"ip route replace {dst_ip} via {nh_ip} dev {dev_name} metric {metric} onlink"
                    append_route_cmd(src_idx, route_append_str)
                    return
                else:
                    raise ValueError(f"Unsupported IP version: {ip_version}")

            nh_idx = next_hops[0]
            nh_name = inv_node_map[nh_idx]
            metric = 100
            route_add(src_name=inv_node_map[src_idx], dst_name=target_node, nh_name=nh_name, metric=metric)

            if len(next_hops) == 2 and redundancy:
                nh_idx = next_hops[1]
                nh_name = inv_node_map[nh_idx]
                metric = 200
                route_add(src_name=inv_node_map[src_idx], dst_name=target_node, nh_name=nh_name, metric=metric)

    # Create new epoch payload (same structure as original)
    new_epoch_data: Dict[str, Any] = {}
    new_epoch_data["time"] = epoch_data.get("time", "")

    try:
        t = parse_epoch_time(new_epoch_data["time"])
        t_new = t - timedelta(seconds=offset_seconds)
        new_epoch_data["time"] = t_new.strftime("%Y-%m-%dT%H:%M:%SZ")
        new_epoch_data["run"] = {}
        for src_name, commands in route_commands.items():
            combined_cmd = join_route_commands_with_sleep(
                commands=commands,
                max_routes_per_epoch=max_routes_per_epoch,
                sleep_seconds=sleep_seconds,
            )
            if combined_cmd:
                new_epoch_data["run"][src_name] = [combined_cmd]
    except ValueError as ve:
        log.warning(f"\t ⚠️ Error parsing time '{new_epoch_data['time']}': {ve}")

    return new_epoch_data

def compute_routes(
    etcd_client,
    epoch_dir: str,
    file_pattern: str,
    out_epoch_dir: str,
    node_type_to_route: list,
    node_type_to_install: list,
    node_type_to_process: list,
    drain_before_break_offset: int,
    link_creation_offset: int,
    ip_version: int,
    etcd_etchosts_prefix: str,
    redundancy: bool,
    routing_metric: str,
    max_routes_per_epoch: int,
    route_batch_sleep_seconds: int,
    report_path: str | None = None,
) -> None:
    # Load config and build node map (same as original)
    log.info("📁 Loading configuration from etcd...")
    nodes = get_prefix_data(etcd_client, "/config/nodes")
    log.info(f"🔎 Found {len(nodes)} nodes in configuration.")
    log.info(f"     - Node type: {node_type_to_process}")
    log.info(f"     - Node type to route: {node_type_to_route}")
    log.info(f"     - Node type to install: {node_type_to_install}")

    
    node_map: Dict[str, int] = {} # node_name -> index in adjacency matrix
    node_type: Dict[str, str] = {} # node_name -> node_type 
    node_to_route: List[str] = []
    node_to_install: List[str] = []

    # Build node map and filter nodes to route/install based on type
    idx = 0
    for name, node_info in nodes.items():
        if node_info.get("type") not in node_type_to_process and "any" not in node_type_to_process:
            continue
        node_map[name] = idx
        node_type[name] = node_info.get("type", "unknown")
        idx += 1
        if node_info.get("type") in node_type_to_route or "any" in node_type_to_route:
            node_to_route.append(name)
        if node_info.get("type") in node_type_to_install or "any" in node_type_to_install:
            node_to_install.append(name)
    inv_node_map = {i: name for name, i in node_map.items()} # for reverse lookup of node names by index

    # Build IP map from etcd prefix
    found_all_ip = True
    ip_map: Dict[str, str] = {}
    for value, meta in etcd_client.get_prefix(etcd_etchosts_prefix):
        node_name = meta.key.decode().split('/')[-1]
        ip_addr = value.decode().strip()
        if ip_addr:
            ip_map[node_name] = ip_addr
        else:
            log.warning(f"⚠️ Empty IP for node '{node_name}' under Etcd prefix '{etcd_etchosts_prefix}'")
            found_all_ip = False

    if not found_all_ip:
        raise ValueError(f"❌ Missing node IP addresses in Etcd under prefix '{etcd_etchosts_prefix}'. Oracle routing can not proceed.")

    num_nodes = len(node_map)
    if len(node_to_route) == 0 or len(node_to_install) == 0:
        raise ValueError("⚠️ Configuration has 0 nodes to route/install.")

    log.info("🛣️ Computing routes ...")
    A_lil = lil_matrix((num_nodes, num_nodes), dtype="float64")  # adjacency matrix for Dijkstra (weights will be 1 or cross_type_penalty for hop-based, or delay-based weights for delay-based)
    unnumbered_file_pattern = file_pattern.replace("*", "??????")
    epoch_files = list_epoch_files(epoch_dir, file_pattern)

    log.info(f"\t 🔎 Found {len(epoch_files)} epoch files to process.")
    if not epoch_files:
        raise FileNotFoundError(f"\t No epoch files found in '{epoch_dir}' with pattern '{file_pattern}'")

    num_epochs = 0
    previous_next_hops: Dict[int, Dict[int, list]] = {}
    report_data: Dict[str, List[Dict[str, Any]]] = {}
    file_counter = 1
    last_inserted_epoc_time: datetime | None = None

    for path in epoch_files:
        with open(path, "r", encoding="utf-8") as f:
            epoch_data = json.load(f)
        original_epoch_time = parse_epoch_time(epoch_data.get("time", ""))

        log.info(f"\t 💾 Processing epoch file: {path} (time: {epoch_data.get('time','UNKNOWN')})")

        # 1) Optional drain-before-break epoch first
        if drain_before_break_offset > 0:
            dbb_epoch_data = compute_routes_single_epoch(
                epoch_data=epoch_data,
                node_map=node_map,
                node_type=node_type,
                A_lil=A_lil,
                node_to_route=node_to_route,
                node_to_install=node_to_install,
                previous_next_hops=previous_next_hops,
                drain_before_break=True,
                offset_seconds=drain_before_break_offset,
                num_nodes=num_nodes,
                inv_node_map=inv_node_map,
                ip_map=ip_map,
                ip_version=ip_version,
                redundancy=redundancy,
                routing_metric=routing_metric,
                max_routes_per_epoch=max_routes_per_epoch,
                sleep_seconds=route_batch_sleep_seconds,
            )
            if dbb_epoch_data.get("run", {}) != {}:
                file_path = unnumbered_file_pattern.replace("??????", f"{file_counter}")
                file_counter += 1
                out_epoch_path = os.path.join(out_epoch_dir, os.path.basename(file_path))
                with open(out_epoch_path, "w", encoding="utf-8") as f_out:
                    json.dump(dbb_epoch_data, f_out, indent=2)
                last_inserted_epoc_time = parse_epoch_time(dbb_epoch_data.get("time", ""))

        # 2) Add original epoch
        if last_inserted_epoc_time is not None and original_epoch_time <= last_inserted_epoc_time:
            raise ValueError(
                "❌ Original epoch time must be strictly greater than the last inserted epoch time "
                f"({epoch_data.get('time')} !> {last_inserted_epoc_time.strftime('%Y-%m-%dT%H:%M:%SZ')})."
            )
        file_path = unnumbered_file_pattern.replace("??????", f"{file_counter}")
        file_counter += 1
        out_epoch_path = os.path.join(out_epoch_dir, os.path.basename(file_path))
        shutil.copyfile(path, out_epoch_path)
        last_inserted_epoc_time = original_epoch_time

        # 3) Add routes to original epoch (shifted earlier by link_creation_offset)
        epoch_route_changes: Dict[str, int] = {}
        new_epoch_data = compute_routes_single_epoch(
            epoch_data=epoch_data,
            node_map=node_map,
            node_type=node_type,
            A_lil=A_lil,
            node_to_route=node_to_route,
            node_to_install=node_to_install,
            previous_next_hops=previous_next_hops,
            drain_before_break=False,
            offset_seconds=-link_creation_offset,
            num_nodes=num_nodes,
            inv_node_map=inv_node_map,
            ip_map=ip_map,
            ip_version=ip_version,
            redundancy = redundancy,
            routing_metric=routing_metric,
            max_routes_per_epoch=max_routes_per_epoch,
            sleep_seconds=route_batch_sleep_seconds,
            route_change_count_by_node=epoch_route_changes,
        )
        epoch_name = os.path.basename(path)
        report_data[epoch_name] = [
            {"name": node_name, "updates": updates}
            for node_name, updates in sorted(epoch_route_changes.items())
        ]
        if new_epoch_data.get("run", {}) != {}:
            file_path = unnumbered_file_pattern.replace("??????", f"{file_counter}")
            file_counter += 1
            out_epoch_path = os.path.join(out_epoch_dir, os.path.basename(file_path))
            with open(out_epoch_path, "w", encoding="utf-8") as f_out:
                json.dump(new_epoch_data, f_out, indent=2)
            last_inserted_epoc_time = parse_epoch_time(new_epoch_data.get("time", ""))

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
            node_type=node_type,
            A_lil=A_lil,
            node_to_route=node_to_route,
            node_to_install=node_to_install,
            previous_next_hops=previous_next_hops,
            drain_before_break=True,
            offset_seconds=drain_before_break_offset,
            num_nodes=num_nodes,
            inv_node_map=inv_node_map,
            ip_map=ip_map,
            ip_version=ip_version,
            redundancy = redundancy,
            routing_metric=routing_metric,
            max_routes_per_epoch=max_routes_per_epoch,
            sleep_seconds=route_batch_sleep_seconds,
        )
        if dbb_epoch_data0.get("run", {}) != {}:
            file_path0 = unnumbered_file_pattern.replace("??????", "0")
            out_epoch_path0 = os.path.join(out_epoch_dir, os.path.basename(file_path0))
            with open(out_epoch_path0, "w", encoding="utf-8") as f_out:
                json.dump(dbb_epoch_data0, f_out, indent=2)

    if report_path:
        with open(report_path, "w", encoding="utf-8") as f_report:
            json.dump(report_data, f_report, indent=2)
        log.info(f"📝 Wrote routing update report to: {report_path}")

# ==========================================
# MAIN
# ==========================================
def main() -> int:
    global link_delay_quantum_ms
    parser = argparse.ArgumentParser(description="Compute routes from epoch files and emit route-injected epoch JSONs (IPv4/IPv6).")

    parser.add_argument("--etcd-host", default=os.getenv("ETCD_HOST", "127.0.0.1"), help="Etcd host (default: env ETCD_HOST or 127.0.0.1)")
    parser.add_argument("--etcd-port", type=int, default=int(os.getenv("ETCD_PORT", 2379)), help="Etcd port (default: env ETCD_PORT or 2379)")
    parser.add_argument("--etcd-user", default=os.getenv("ETCD_USER", None), help="Etcd user (default: env ETCD_USER or None)")
    parser.add_argument("--etcd-password", default=os.getenv("ETCD_PASSWORD", None), help="Etcd password (default: env ETCD_PASSWORD or None)")
    parser.add_argument("--etcd-ca-cert", default=os.getenv("ETCD_CA_CERT", None), help="Etcd CA certificate (default: env ETCD_CA_CERT or None)")

    parser.add_argument("--epoch-dir", help="Epoch directory, takes precedence over Etcd.")
    parser.add_argument("--file-pattern", help="Epoch filename pattern, takes precedence over Etcd.")
    parser.add_argument("--out-epoch-dir", help="Output dir for processed epochs with route injection.")
    parser.add_argument("--report", help="Output JSON file for per-original-epoch routing update statistics. If omitted, no report file is created.")

    parser.add_argument("--node-type-to-route", default="", help="Comma-separated node types to route to (default: --node-type). Matches against node 'type' in config. Use 'any' to route to all nodes.")
    parser.add_argument("--node-type-to-install", default="", help="Comma-separated node types to install routes on (default: --node-type). Matches against node 'type' in config. Use 'any' to install on all nodes.")
    parser.add_argument("--node-type", default="any", help="Comma-separated node types to include in the Dijkstra graph (default: any). Matches against node 'type' in config. Use 'any' to install on all nodes.")
    parser.add_argument("--node-type-no-forward", default="user", help="Comma-separated node types to treat as hosts not suporting IP forwarding (default: user). Matches against node 'type' in config.")
    parser.add_argument("--drain-before-break-offset", type=int, default=0, help="Seconds offset for drain-before-break epoch. If 0, no separate drain-before-break epoch is emitted.")
    parser.add_argument("--link-creation-offset", type=int, default=1, help="Seconds offset for route replacement after link creation. Default: 1 second after link creation.")
    parser.add_argument("--redundancy", action="store_true", help="Whether to compute secondary next hops for redundancy (default: False).")
    parser.add_argument("--ip-version", type=int, choices=[4, 6], default=4, help="Generate IPv4 or IPv6 route commands. Default: 4 (IPv4).")
    parser.add_argument("--routing-metrics", choices=["hops","delay"], default="hops", help="Whether to use hop count or delay as routing metric (default: hops)")
    parser.add_argument("--max-routes-per-epoch", type=int, default=50, help="Maximum number of route commands before inserting a sleep in the combined command string. If <=0, no sleeps are inserted. Default: 50.")
    parser.add_argument("--route-batch-sleep-seconds", type=int, default=1, help="Seconds to sleep between route batches inside a single command string. Default: 1.")
    parser.add_argument("--link-delay-quantum-ms", type=int, default=5, help="Delay quantum for delay-based routing metric. Costs are rounded up to the nearest multiple of this value. Default: 5ms.")

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

    
    # node type checks
    if args.node_type_to_route == "":
            args.node_type_to_route = args.node_type
    if args.node_type_to_install == "":
        args.node_type_to_install = args.node_type
    node_type_to_process = set(args.node_type.split(","))
    node_type_to_route_list = set(args.node_type_to_route.split(","))
    node_type_to_install_list = set(args.node_type_to_install.split(","))
    
    if "any" in node_type_to_process:
        pass  # no filtering needed
    else:
        if not node_type_to_route_list.issubset(node_type_to_process):
            log.error("❌ --node-type-to-route contains types not in global --node-type.")
            return 5
        if not node_type_to_install_list.issubset(node_type_to_process):
            log.error("❌ --node-type-to-install contains types not in global --node-type.")
            return 6
    
    # epoch dir / pattern resolution
    if args.route_batch_sleep_seconds < 0:
        log.error("❌ --route-batch-sleep-seconds must be >= 0.")
        return 7

    if args.link_delay_quantum_ms <= 0:
        log.error("❌ --link-delay-quantum-ms must be a positive integer.")
        return 8
    link_delay_quantum_ms = args.link_delay_quantum_ms


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
            node_type_to_route=node_type_to_route_list,
            node_type_to_install=node_type_to_install_list,
            node_type_to_process=node_type_to_process,
            drain_before_break_offset=args.drain_before_break_offset,
            link_creation_offset=args.link_creation_offset,
            ip_version=args.ip_version,
            etcd_etchosts_prefix="/config/etchosts/" if args.ip_version == 4 else "/config/etchosts6/",
            redundancy=args.redundancy if args.drain_before_break_offset == 0 else True,  # redundancy needed with drain-before-break
            routing_metric=args.routing_metrics if args.routing_metrics else "hops",
            max_routes_per_epoch=args.max_routes_per_epoch,
            route_batch_sleep_seconds=args.route_batch_sleep_seconds,
            report_path=args.report,
        )
    except Exception as e:
        log.error(f"❌ Error during route computation: {e}")
        return 1

    log.info("👍 Route computation completed.")
    log.info(f"👉 Processed epochs with routing info are in directory: {args.out_epoch_dir}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
