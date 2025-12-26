#!/usr/bin/env python3
import os
import shlex
import etcd3
import subprocess
import json
import sys

# ==========================================
# 🚩 CONFIGURATION
# ==========================================
ETCD_HOST = os.getenv('ETCD_HOST', '127.0.0.1')
ETCD_PORT = int(os.getenv('ETCD_PORT', 2379))

# Initialize Etcd
try:
    etcd = etcd3.client(host=ETCD_HOST, port=ETCD_PORT)
    # Simple check to ensure connection is valid
    etcd.status() 
except Exception as e:
    print(f"❌ Failed to connect to Etcd at {ETCD_HOST}:{ETCD_PORT}")
    print(f"   Error: {e}")
    sys.exit(1)

def get_prefix_data(prefix):
    """Helper to fetch and parse JSON data from Etcd prefixes."""
    data = {}
    for value, metadata in etcd.get_prefix(prefix):
        key = metadata.key.decode('utf-8').split('/')[-1]
        try:
            data[key] = json.loads(value.decode('utf-8'))
        except json.JSONDecodeError:
            pass
    return data

# ==========================================
# 1. LOAD CONFIGURATION
# ==========================================
print(f"📁 Connecting to Etcd at {ETCD_HOST}:{ETCD_PORT}...")

# Fetch all relevant configuration
satellites = get_prefix_data('/config/satellites/')
users = get_prefix_data('/config/users/')
grounds = get_prefix_data('/config/grounds/')
hosts = get_prefix_data('/config/hosts/')

# Merge satellites and users into one list of nodes to clean up
all_nodes = {**satellites, **users, **grounds}
print(f"🔎 Found {len(all_nodes)} nodes (satellites + users + grounds) to clean up.")

if not all_nodes:
    print("⚠️  No nodes found in Etcd. Nothing to do.")

# ==========================================
# 2. DELETE CONTAINERS (Bash Logic Merged)
# ==========================================
print("\n🧹 Starting Cleanup Process...")
print("-" * 50)

for name, node in all_nodes.items():
    node_host = node.get('host')
    
    # Validation: Does the host exist in config?
    if node_host not in hosts:
        print(f"⚠️  Skipping {name}: Host '{node_host}' not found in /config/hosts")
        continue

    host_info = hosts[node_host]
    
    # Extract SSH Details
    # Uses 'ip' from config, falls back to 'host-X' alias if missing
    ssh_ip = host_info.get('ip', node_host) 
    ssh_user = host_info.get('ssh_user', 'ubuntu')
    ssh_key = host_info.get('ssh_key', '~/.ssh/id_rsa')


    print(f"🔹 Processing {name} on {node_host} ({ssh_ip})...")

    # ----------------------------------------
    # STEP 1: Check if container exists
    # ----------------------------------------
    # Bash Equivalent: docker ps -a --format '{{.Names}}' | grep -Fxq "$SAT_NAME"
    check_cmd = f"docker ps -a --format '{{{{.Names}}}}' | grep -Fxq '{name}'"
    
    print(f"   🔍 Checking if container '{name}' exists...")
    check_proc = subprocess.run(
        ["ssh", "-i", ssh_key, f"{ssh_user}@{ssh_ip}", "-C", check_cmd],
        capture_output=True,
        text=True
    )

    if check_proc.returncode != 0:
        print(f"   ❌ Container '{name}' does not exist on {node_host}. Skipping.")
        continue

    # ----------------------------------------
    # STEP 2: Stop and Remove
    # ----------------------------------------
    # Bash Equivalent: docker stop $SAT_NAME && docker rm $SAT_NAME
    print(f"   ⏹️  Stopping and removing '{name}'...")
    
    try:
        # We run stop and rm in a single SSH session for efficiency
        stop_rm_cmd = f"docker rm -f {name}"
        
        del_proc = subprocess.run(
            ["ssh", "-i", ssh_key, f"{ssh_user}@{ssh_ip}", "-C", stop_rm_cmd],
            check=True,
            stdout=subprocess.DEVNULL, # Suppress remote docker output
            stderr=subprocess.PIPE     # Capture errors if they happen
        )
        if del_proc.returncode == 0:
            print(f"   ✅ Successfully deleted {name}")
        else:
            print(f"   ❌ Failed to delete {name}. Return code: {del_proc.returncode}")
        
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr.decode().strip()
        print(f"   ❌ Failed to delete {name}. Error: {error_msg}")

# ==========================================
# CLEAN ETCD ENTRIES
# ==========================================
print("\n🧼 Cleaning up Etcd entries...")
prefixes = ['/config/links/', '/config/run/', '/config/etchosts/', '/config/satellites/', '/config/users/', '/config/grounds/']
for prefix in prefixes:
    print(f"   ➞ Deleting keys with prefix {prefix} ...")
    etcd.delete_prefix(prefix)

print("-" * 50)
print("✅ Global Cleanup Complete.")