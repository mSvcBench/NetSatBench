#!/usr/bin/env python3
#network-configuration.py


import etcd3
import subprocess
import json
import os
import sys
import re

# ==========================================
# 🚩 CONFIGURATION
# ==========================================
ETCD_HOST = '10.0.1.215'
ETCD_PORT = 2379
ENABLE_TC = True    # Apply Bandwidth/Latency rules
ENABLE_ISIS = True  # Apply FRR/IS-IS configuration

try:
    etcd = etcd3.client(host=ETCD_HOST, port=ETCD_PORT)
except Exception as e:
    print(f"❌ Failed to initialize Etcd client: {e}")
    sys.exit(1)

def get_prefix_data(prefix):
    """Fetch all keys under a prefix and return a dict."""
    data = {}
    for value, metadata in etcd.get_prefix(prefix):
        key = metadata.key.decode('utf-8').split('/')[-1]
        try:
            data[key] = json.loads(value.decode('utf-8'))
        except json.JSONDecodeError:
            pass
    return data

def get_links():
    """Fetch the list of links from the single /config/links key."""
    value, _ = etcd.get('/config/links')
    if value:
        try:
            return json.loads(value.decode('utf-8'))
        except:
            return []
    return []

def run_remote_cmd(host, user, cmd):
    """Executes a command on the remote host via SSH."""
    ssh_cmd = ["ssh", f"{user}@{host}", cmd]
    try:
        subprocess.run(ssh_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        return True
    except subprocess.CalledProcessError as e:
        print(f"   ❌ SSH Error on {host}: {e.stderr.decode().strip()}")
        return False

def run_docker_exec(host, user, container, cmd_inside_container):
    """Helper to run 'docker exec' via SSH."""
    full_cmd = f"docker exec {container} {cmd_inside_container}"
    return run_remote_cmd(host, user, full_cmd)

# ==========================================
# LOAD DATA
# ==========================================
print(f"📡 Connecting to Etcd at {ETCD_HOST}:{ETCD_PORT}...")
satellites = get_prefix_data('/config/satellites/')
users = get_prefix_data('/config/users/')
links = get_links()
hosts = get_prefix_data('/config/hosts/')

# ⚠️ MERGE USERS AND SATELLITES
# This ensures we treat ground1 exactly like a satellite for config purposes
all_nodes = {**satellites, **users}

print(f"   Found {len(all_nodes)} nodes ({len(satellites)} sats, {len(users)} users), {len(links)} links.")

# ==========================================
# STEP 1: Apply Traffic Control (TC)
# ==========================================
if ENABLE_TC:
    print("\n  Applying Traffic Control Rules...")
    
    for link in links:
        bw = link.get('bw')
        burst = link.get('burst')
        latency = link.get('latency')
        
        if not (bw and burst and latency): continue

        ep1 = link.get('endpoint1')
        ep2 = link.get('endpoint2')
        
        # FIXED: Check against all_nodes, not just satellites
        if ep1 not in all_nodes or ep2 not in all_nodes: 
            print(f"   ⚠️ Skipping link {ep1}-{ep2}: Node not found.")
            continue

        ep1_ant = link.get('endpoint1_antenna')
        ep2_ant = link.get('endpoint2_antenna')
        
        # Interface Naming: {RemoteSat}_a{RemoteAntenna}
        if_on_ep1 = f"{ep2}_a{ep2_ant}"
        if_on_ep2 = f"{ep1}_a{ep1_ant}"

        # Get Host/User info from the merged dictionary
        host1 = all_nodes[ep1]['host']
        host2 = all_nodes[ep2]['host']
        
        user1 = hosts.get(host1, {}).get('ssh_user', 'ubuntu')
        user2 = hosts.get(host2, {}).get('ssh_user', 'ubuntu')

        print(f"   🔹 {ep1} <-> {ep2}: {bw}, {latency}")
        
        def apply_tc(h, u, c, iface):
            cmd = f"bash -c 'tc qdisc del dev {iface} root 2>/dev/null || true; tc qdisc add dev {iface} root tbf rate {bw} burst {burst} latency {latency}'"
            run_docker_exec(h, u, c, cmd)

        apply_tc(host1, user1, ep1, if_on_ep1)
        apply_tc(host2, user2, ep2, if_on_ep2)

else:
    print("\n🚫 Skipping Traffic Control (ENABLE_TC = False)")

# ==========================================
# STEP 2: Configure IS-IS (FRRouting)
# ==========================================
if ENABLE_ISIS:
    print("\n🚀 Configuring FRR/IS-IS...")

    # FIXED: Iterate over all_nodes so ground1 gets configured too
    for name, node in all_nodes.items():
        
        # 1. Parse Config
        n_antennas = int(node.get("N_Antennas", 1)) # Default to 1 (safe for users)
        host_str = node.get("host", "host-1")
        
        # 2. Generate System ID from Name (sat3 -> 0003, ground1 -> 0201)
        # We need a unique ID for IS-IS.
        match = re.search(r'(\d+)', name)
        if match:
            node_num = int(match.group(1))
            if name.startswith("ground") or name.startswith("user"):
                # Offset ground station IDs to avoid conflict with sats
                # ground1 -> ID 201
                sys_id_val = 200 + node_num
            else:
                sys_id_val = node_num
        else:
            # Fallback for names without numbers
            sys_id_val = 9999 

        sys_id = f"{sys_id_val:04d}"

        # 3. Parse Area ID (host-1 -> 0001)
        try:
            host_num = int(host_str.split('-')[-1])
        except:
            host_num = 1
        net_id = f"{host_num:04d}"

        # 4. Parse Subnets
        # Note: ground1 in your JSON has ["192.168.201.1/32"]
        raw_cidr = node.get("sat_net_cidr")
        base_prefix_str = ""
        
        if isinstance(raw_cidr, list) and len(raw_cidr) > 0:
            # Take the first IP to derive the subnet
            # 192.168.3.1/32 -> 192.168.3
            base_prefix_str = raw_cidr[0].split('/')[0].rsplit('.', 1)[0]
        elif isinstance(raw_cidr, str):
            base_prefix_str = raw_cidr.split('/')[0].rsplit('.', 1)[0]
        else:
            # Auto-gen logic if missing (matches agent logic)
            if name.startswith("ground"):
                 base_prefix_str = f"192.168.{200+node_num}"
            else:
                 base_prefix_str = f"192.168.{node_num}"

        subnet_cidr = f"{base_prefix_str}.0/24"
        antennas = [str(i) for i in range(1, n_antennas + 1)]
        
        user = hosts.get(host_str, {}).get('ssh_user', 'ubuntu')
        
        print(f"   📱 Configuring IS-IS on {name} (Area {net_id}, SysID {sys_id})...")

        args = [net_id, sys_id, subnet_cidr] + antennas
        args_str = " ".join(args)
        
        cmd = f"/agent/configure-isis.sh {args_str}"
        run_docker_exec(host_str, user, name, cmd)

print("\n✅ Configuration Complete.")