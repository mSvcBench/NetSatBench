#!/usr/bin/env python3
import json
import re
import sys
import etcd3
from typing import Dict, Any
import logging

log = logging.getLogger("nsb-logger")

# ==========================================
# ETCD CONNECTION
# ==========================================
def connect_etcd(etcd_host: str, etcd_port: int, etcd_user=None, etcd_password=None, etcd_ca_cert=None):
    try:
        if etcd_user and etcd_password:
            return etcd3.client(host=etcd_host, port=etcd_port, user=etcd_user, password=etcd_password, ca_cert=etcd_ca_cert)
        else:
            return etcd3.client(host=etcd_host, port=etcd_port)
    except Exception as e:
        log.error(f"❌ Failed to initialize Etcd client: {e}")
        sys.exit(1)

# ==========================================
# 🧮 UNIT CONVERSION HELPERS
# ==========================================
def parse_cpu(value) -> float:
    if not value: return 0.0
    val = str(value)
    if val.endswith('m'):
        try:
            return float(val[:-1]) / 1000.0
        except ValueError:
            return 0.0
    try:
        return float(val)
    except ValueError:
        return 0.0

def parse_mem(value) -> float:
    if not value: return 0.0
    val = str(value).strip()
    units = {
        'Ti': 1024.0,  'Gi': 1.0, 'Mi': 1.0/1024.0, 'Ki': 1.0/1048576.0, 
        'TiB': 1024.0, 'GiB': 1.0,'MiB': 1.0/1024.0, 'KiB': 1.0/1048576.0 , 
        'T': 1024.0, 'G': 1.0, 'M': 1.0/1024.0, 'K': 1.0/1048576.0
    }
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
#  SCHEDULING LOGIC
# ==========================================
def schedule_workers(config_data: Dict[str, Any], etcd_client: Any) -> Dict[str, Any]:

    log.info("⚙️  Starting scheduling logic...")
    all_nodes = config_data.get('nodes', {})
    workers = get_prefix_data(etcd_client, '/config/workers/')
    
    for name, cfg in all_nodes.items():
        cpu_req = parse_cpu(cfg.get('cpu-request', 0.0))
        mem_req = parse_mem(cfg.get('mem-request', 0.0))
        cpu_lim = parse_cpu(cfg.get('cpu-limit', 0.0))
        mem_lim = parse_mem(cfg.get('mem-limit', 0.0))
 
        #--- Check if already assigned ---
        assigned_worker = cfg.get('worker', None)  
        if assigned_worker:
            if assigned_worker in workers:
                # Deduct resources from assigned worker
                if parse_cpu(workers[assigned_worker].get('cpu-used', 0)) + cpu_req > parse_cpu(workers[assigned_worker].get('cpu')):
                    log.warning(f"    ⚠️ Warning: Worker {assigned_worker} overcommitted on CPU for node {name}!")
                if parse_mem(workers[assigned_worker].get('mem-used', 0)) + mem_req > parse_mem(workers[assigned_worker].get('mem')):
                    log.warning(f"    ⚠️ Warning: Worker {assigned_worker} overcommitted on MEM for node {name}!")
                workers[assigned_worker]['cpu-used'] = parse_cpu(workers[assigned_worker].get('cpu-used', 0)) + cpu_req
                workers[assigned_worker]['mem-used'] = parse_mem(workers[assigned_worker].get('mem-used', 0)) + mem_req
            else:
                log.warning(f"    ⚠️ Warning: Assigned worker {assigned_worker} for node {name} not found in workers list!")

    #  Calculate average node requirements (for capacity prediction)
    total_cpu_req = 0.0
    total_mem_req = 0.0

    # Count zero-resource nodes and pre-assigned nodes separately
    zero_resource_node_count = 0
    pre_assigned_node_count = 0
    for n_name, n_cfg in all_nodes.items():
        cpu_req = parse_cpu(n_cfg.get('cpu-request', 0.0))
        mem_req = parse_mem(n_cfg.get('mem-request', 0.0))
        is_pre_assigned = bool(n_cfg.get('worker', None))
        is_zero_resource = (cpu_req == 0.0 and mem_req == 0.0)
        total_cpu_req += cpu_req
        total_mem_req += mem_req
        if is_pre_assigned:
            pre_assigned_node_count += 1
        elif is_zero_resource:
            zero_resource_node_count += 1

    non_zero = [
        (parse_cpu(c.get('cpu-request', 0)), parse_mem(c.get('mem-request', 0)))
        for c in all_nodes.values()
        if parse_cpu(c.get('cpu-request', 0)) > 0 or parse_mem(c.get('mem-request', 0)) > 0
    ]
    avg_cpu = sum(c for c, m in non_zero) / len(non_zero) if non_zero else 0.1
    avg_mem = sum(m for c, m in non_zero) / len(non_zero) if non_zero else 0.1
    
    workers_resources = []
    for name, cfg in workers.items():
        w_cpu = parse_cpu(cfg.get('cpu', 0.0))
        w_mem = parse_mem(cfg.get('mem', 0.0))
        
        # Calculate hardware capacity based on resource-consuming nodes
        max_nodes_by_cpu = int(w_cpu / avg_cpu) 
        max_nodes_by_mem = int(w_mem / avg_mem)
        max_nodes_by_resources = max(1, min(max_nodes_by_cpu, max_nodes_by_mem))

        # Distribute zero-resource nodes proportionally across workers (ceiling division for fair distribution)
        # Pre-assigned nodes are already placed and don't consume new scheduling slots
        worker_count = max(len(workers), 1)
        extra_zero_resource = -(-zero_resource_node_count // worker_count)  # ceiling division

        # max-nodes = resource-based capacity + zero-resource nodes share 
        max_nodes = max_nodes_by_resources + extra_zero_resource 
        log.info(f"Worker: {name} | CPU: {w_cpu} | MEM: {round(w_mem,4)}GiB "
                 f"Max Nodes: {max_nodes} (CPU-based: {max_nodes_by_cpu}, "
                 f"MEM-based: {max_nodes_by_mem}, Zero-Resource Share: {extra_zero_resource})")
       
        workers_resources.append({
            'name': name,
            'data': cfg,
            'cpu': parse_cpu(cfg.get('cpu', 0.0)),
            'mem': parse_mem(cfg.get('mem', 0.0)),
            'cpu-used': parse_cpu(cfg.get('cpu-used', 0.0)),
            'mem-used': parse_mem(cfg.get('mem-used', 0.0)),
            "assigned-nodes": [],
            "max-nodes": max_nodes
        })

    def get_worker_score(w):
        free_cpu = w['cpu'] - w['cpu-used']
        free_mem_gib = (w['mem'] - w['mem-used']) 
        return free_cpu + (free_mem_gib / 2.0) # rule of thumb 1 CPU 2 GiB for best balancing

    def get_node_score(n):
        return n['cpu_req'] + (n['mem_req']/2.0) # rule of thumb 1 CPU 2 GiB for best balancing
    
    all_schedulable_nodes = []
    for name, cfg in all_nodes.items():
        if 'worker' in cfg:
            continue  # already assigned
        cpu_req = parse_cpu(cfg.get('cpu-request',0.0))
        mem_req = parse_mem(cfg.get('mem-request',0.0))
        all_schedulable_nodes.append({
            'name': name,
            'data': cfg, 
            'cpu_req': cpu_req,
            'mem_req': mem_req
        })

    # --- Sort nodes by resource demand ---
    all_schedulable_nodes.sort(key=get_node_score, reverse=True)
    
    for node in all_schedulable_nodes:
        workers_resources.sort(key=get_worker_score, reverse=True)
        
        assigned = False
        for worker in workers_resources:
            free_cpu = worker['cpu'] - worker['cpu-used']
            free_mem = worker['mem'] - worker['mem-used']
            if free_cpu >= node['cpu_req'] and free_mem >= node['mem_req'] and len(worker["assigned-nodes"]) < worker["max-nodes"]:
                # Allocate logic
                worker['cpu-used'] += node['cpu_req']   
                worker['mem-used'] += node['mem_req'] 
                worker["assigned-nodes"].append(node['name'])
                # Update the Config Dictionary directly
                node['data']['worker'] = worker['name']
                assigned = True
                log.info(f"    ➞ Assigned Node: {node['name']} to Worker: {worker['name']} (CPU Req: {node['cpu_req']}, MEM Req: {round(node['mem_req'],4)}GiB)")
                break
        if not assigned:
            # Not enough resource found. Overcommit node with highest free resources but respect max-nodes
            for worker in workers_resources:
                if len(worker["assigned-nodes"]) < worker["max-nodes"]:
                    best_worker = worker
                    assigned = True
                    break
            if assigned:
                best_worker['cpu-used'] += node['cpu_req'] if node['cpu_req']> 0.0 else 0.000001  # avoid zero cpu consumption for round-robin scheduling 
                best_worker['mem-used'] += node['mem_req'] if node['mem_req'] > 0.0 else 0.000001  # avoid zero mem consumption for round-robin scheduling
                best_worker["assigned-nodes"].append(node['name'])
                node['data']['worker'] = best_worker['name']
                log.warning(f"    ⚠️ Overcommitted Node: {node['name']} to Worker: {best_worker['name']} (CPU Req: {node['cpu_req']}, MEM Req: {node['mem_req']}GiB)")
            else:
                log.error(f"❌ Unable to schedule Node: {node['name']}. No available workers with available underlay IP address, extend sat-vnet-cidr.")
                sys.exit(1)
    # update worker config in etcd with usage stats
    for worker in workers_resources:
        worker_cfg = worker['data'].copy()
        worker_cfg['cpu'] = worker['cpu']
        # Update usage fields
        worker_cfg['cpu-used'] = round(worker['cpu-used'], 2) 
        used_gib = round(worker['mem-used'], 4) # Convert mem-used back to GiB string 
        worker_cfg['mem-used'] = f"{used_gib}GiB"
        
        # Write to Etcd under /config/workers/{worker_name}
        key = f"/config/workers/{worker['name']}"
        etcd_client.put(key, json.dumps(worker_cfg))



    log.info("✅ Scheduling Completed.")
    return config_data