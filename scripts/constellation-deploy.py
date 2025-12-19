#!/usr/bin/env python3
from unittest import result
import etcd3
import subprocess
import json
import os
import sys
import shlex

# ==========================================
# 🚩 CONFIGURATION
# ==========================================
# get ETCD_HOST, ETCD_PORT and SAT_HOST_BRIDGE_NAME from environment variables if set
ETCD_HOST = os.getenv('ETCD_HOST', '127.0.0.1')
ETCD_PORT = int(os.getenv('ETCD_PORT', 2379))


try:
    print(f"📁 Connecting to Etcd at {ETCD_HOST}:{ETCD_PORT}...")
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


# Fetch Satellites AND Users
satellites = get_prefix_data('/config/satellites/')
users = get_prefix_data('/config/users/')
grounds = get_prefix_data('/config/grounds/')
hosts = get_prefix_data('/config/hosts/')

# Merge them into one dictionary for processing
all_nodes = {**satellites, **users, **grounds}

print(f"🔎    Found {len(satellites)} satellites, {len(users)} users, and {len(grounds)} grounds in Etcd.")

if not all_nodes:
    print("⚠️  Warning: No nodes found. Run 'init.py' to populate Etcd first.")

# ==========================================
# 2. CREATE CONTAINERS (Satellites + Users + Grounds)
# ==========================================
for name, node in all_nodes.items():
    print(f"🛰️ Creating node: {name}")
    
    # 1. Validate Host
    node_host = node.get('host')
    if node_host not in hosts:
        print(f"❌ Error: Node {name} assigned to unknown host '{node_host}'")
        continue
        
    host_info = hosts[node_host]
    ssh_user = host_info.get('ssh_user', 'ubuntu')
    ssh_key = host_info.get('ssh_key', '~/.ssh/id_rsa')
    sat_bridge = host_info.get('sat-vnet', 'sat-vnet')
    
    # Get Image (Default to 7.6 if missing)
    image = node.get('image', 'msvcbench/sat-container:latest')

    # 3. Run Creation Script
    # Usage: ./create-sat.sh <SAT_NAME> [SAT_HOST] [SSH_USERNAME] [SSH_KEY_PATH] [ETCD_HOST] [ETCD_PORT] [SAT_HOST_BRIDGE_NAME] [CONTAINER_IMAGE]
    cmd = ['scripts/create-sat.sh', name, node_host, ssh_user, ssh_key, ETCD_HOST, str(ETCD_PORT), sat_bridge, image]

    #print("Running:", " ".join(shlex.quote(x) for x in cmd))

    try:
        res = subprocess.run(
            cmd,
            check=True,
            text=True,              # <-- strings, not bytes
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        #print("📄 STDOUT:\n", res.stdout)
        #print("📄 STDERR:\n", res.stderr)

    except subprocess.CalledProcessError as e:
        print(f"❌ Failed to create {name} (exit code {e.returncode})")
        print("STDOUT:\n", e.stdout)
        print("STDERR:\n", e.stderr)


print("\n✅ Constellation Build Complete.")