#!/usr/bin/env python3
import json
import re
import sys
import etcd3
from typing import Dict, Any

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
        print(f"❌ Failed to initialize Etcd client: {e}")
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
            print(f"⚠️ Warning: Could not parse JSON for key {key} under {prefix}")
    return data

# ==========================================
#  SCHEDULING LOGIC
# ==========================================
def schedule_workers(config_data: Dict[str, Any], etcd_client: Any) -> Dict[str, Any]:

    print("⚙️  Starting scheduling logic...")

    satellites = config_data.get('satellites', {})
    users = config_data.get('users', {})
    grounds = config_data.get('grounds', {})
    all_nodes = {**satellites, **users, **grounds}
    workers = get_prefix_data(etcd_client, '/config/workers/')
    

    nodes_to_schedule = []
    for name, cfg in all_nodes.items():
        cpu_req = parse_cpu(cfg.get('cpu-request',0.0))
        cfg['cpu-request'] = cpu_req # remove unit strings

        mem_req = parse_mem(cfg.get('mem-request',0.0))
        cfg['mem-request'] = f"{int(mem_req * 1024)}MiB" # store as MiB string

        print(f"    Node: {name} | CPU Req: {cpu_req} | MEM Req: {mem_req}GiB | req worker: {cfg.get('worker', 'None')}")
        
        #--- Check if already assigned ---
        assigned_worker = cfg.get('worker', None)  
        if assigned_worker:
            if assigned_worker in workers:
                # Deduct resources from assigned worker
                if workers[assigned_worker]['cpu-used'] + cpu_req > parse_cpu(workers[assigned_worker].get('cpu')):
                    print(f"    ⚠️ Warning: Worker {assigned_worker} overcommitted on CPU for node {name}!")
                if workers[assigned_worker]['mem-used'] + mem_req > parse_mem(workers[assigned_worker].get('mem')):
                    print(f"    ⚠️ Warning: Worker {assigned_worker} overcommitted on MEM for node {name}!")
                workers[assigned_worker]['cpu-used'] += cpu_req
                workers[assigned_worker]['mem-used'] += mem_req
        else:
            nodes_to_schedule.append((name, cfg, cpu_req, mem_req))

    workers_resources = []
    for name, cfg in workers.items():
        workers_resources.append({
            'name': name,
            'data': cfg,
            'cpu': parse_cpu(cfg.get('cpu', 0.0)),
            'mem': parse_mem(cfg.get('mem', 0.0)),
            'cpu-used': parse_cpu(cfg.get('cpu-used', 0.0)),
            'mem-used': parse_mem(cfg.get('mem-used', 0.0))
        })

    # if not workers_resources:
    #     print("❌ Scheduler Aborted: No workers found in config.")
    #     return config_data

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
            if free_cpu >= node['cpu_req'] and free_mem >= node['mem_req']:
                # Allocate logic
                worker['cpu-used'] += node['cpu_req']
                worker['mem-used'] += node['mem_req']
                
                # Update the Config Dictionary directly
                node['data']['worker'] = worker['name']
                assigned = True
                print(f"    ✅ Assigned Node: {node['name']} to Worker: {worker['name']} (CPU Req: {node['cpu_req']}, MEM Req: {node['mem_req']}GiB)")
                break
        if not assigned:
            # Not enough resource found. Overcommit node with highest free resources
            best_worker = workers_resources[0]
            best_worker['cpu-used'] += node['cpu_req']
            best_worker['mem-used'] += node['mem_req']
            node['data']['worker'] = best_worker['name']
            print(f"    ⚠️ Overcommitted Node: {node['name']} to Worker: {best_worker['name']} (CPU Req: {node['cpu_req']}, MEM Req: {node['mem_req']}GiB)")

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



    print("✅ Scheduling logic finished.")
    return config_data