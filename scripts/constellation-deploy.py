#!/usr/bin/env python3
import etcd3
import subprocess
import json
import os
import sys

# ==========================================
# 🚩 CONFIGURATION
# ==========================================
ETCD_HOST = '10.0.1.215'
ETCD_PORT = 2379

try:
    etcd = etcd3.client(host=ETCD_HOST, port=ETCD_PORT)
except Exception as e:
    print(f"❌ Failed to initialize Etcd client: {e}")
    sys.exit(1)

def get_prefix_data(prefix):
    data = {}
    for value, metadata in etcd.get_prefix(prefix):
        key = metadata.key.decode('utf-8').split('/')[-1]
        try:
            data[key] = json.loads(value.decode('utf-8'))
        except json.JSONDecodeError:
            print(f"⚠️ Warning: Could not parse JSON for key {key}")
    return data

# ==========================================
# 1. LOAD CONFIGURATION
# ==========================================
print(f"📡 Connecting to Etcd at {ETCD_HOST}:{ETCD_PORT}...")

# Fetch Satellites AND Users
satellites = get_prefix_data('/config/satellites/')
users = get_prefix_data('/config/users/')
hosts = get_prefix_data('/config/hosts/')

# Merge them into one dictionary for processing
all_nodes = {**satellites, **users}

print(f"   Found {len(satellites)} satellites and {len(users)} users in Etcd.")

if not all_nodes:
    print("⚠️  Warning: No nodes found. Run 'constellation-conf.py' to populate Etcd first.")

# ==========================================
# 2. CREATE CONTAINERS (Satellites + Users)
# ==========================================
for name, node in all_nodes.items():
    print(f"➞ Creating node: {name}")
    
    # 1. Validate Host
    host_key = node.get('host')
    if host_key not in hosts:
        print(f"❌ Error: Node {name} assigned to unknown host '{host_key}'")
        continue
        
    host_info = hosts[host_key]
    ssh_user = host_info.get('ssh_user', 'ubuntu')
    
    # 2. Get Config params
    n_antennas = str(node.get('n_antennas', 1)) # Default users to 1 antenna if missing
    
    # Get Image (Default to 7.6 if missing)
    image = node.get('image', 'shahramdd/sat:7.6')

    # 3. Run Creation Script
    # Usage: ./create-sat.sh <NAME> <N_ANT> <HOST> <USER> <IMAGE>
    cmd = ['./create-sat.sh', name, n_antennas, host_key, ssh_user, image]
    
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"❌ Failed to create {name}: {e}")

print("\n✅ Constellation Build Complete.")