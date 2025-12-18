#!/usr/bin/env python3
import etcd3
import subprocess
import json
import sys

# ==========================================
# 🚩 CONFIGURATION
# ==========================================
ETCD_HOST = '10.0.1.215'
ETCD_PORT = 2379

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
print(f"📡 Connecting to Etcd at {ETCD_HOST}:{ETCD_PORT}...")

# Fetch all relevant configuration
satellites = get_prefix_data('/config/satellites/')
users = get_prefix_data('/config/users/')
hosts = get_prefix_data('/config/hosts/')

# Merge satellites and users into one list of nodes to clean up
all_nodes = {**satellites, **users}

print(f"🔎 Found {len(all_nodes)} nodes (satellites + users) to clean up.")

if not all_nodes:
    print("⚠️  No nodes found in Etcd. Nothing to do.")
    sys.exit(0)

# ==========================================
# 2. DELETE CONTAINERS (Bash Logic Merged)
# ==========================================
print("\n🧹 Starting Cleanup Process...")
print("-" * 50)

for name, node in all_nodes.items():
    host_id = node.get('host')
    
    # Validation: Does the host exist in config?
    if host_id not in hosts:
        print(f"⚠️  Skipping {name}: Host '{host_id}' not found in /config/hosts")
        continue

    host_info = hosts[host_id]
    
    # Extract SSH Details
    # Uses 'ip' from config, falls back to 'host-X' alias if missing
    ssh_ip = host_info.get('ip', host_id) 
    ssh_user = host_info.get('ssh_user', 'ubuntu')
    ssh_target = f"{ssh_user}@{ssh_ip}"

    print(f"🔹 Processing {name} on {host_id} ({ssh_ip})...")

    # ----------------------------------------
    # STEP 1: Check if container exists
    # ----------------------------------------
    # Bash Equivalent: docker ps -a --format '{{.Names}}' | grep -Fxq "$SAT_NAME"
    check_cmd = f"docker ps -a --format '{{{{.Names}}}}' | grep -Fxq '{name}'"
    
    check_proc = subprocess.run(
        ['ssh', ssh_target, check_cmd],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

    if check_proc.returncode != 0:
        print(f"   ❌ Container '{name}' does not exist on {host_id}. Skipping.")
        continue

    # ----------------------------------------
    # STEP 2: Stop and Remove
    # ----------------------------------------
    # Bash Equivalent: docker stop $SAT_NAME && docker rm $SAT_NAME
    print(f"   ⏹️  Stopping and removing '{name}'...")
    
    try:
        # We run stop and rm in a single SSH session for efficiency
        stop_rm_cmd = f"docker stop {name} && docker rm {name}"
        
        subprocess.run(
            ['ssh', ssh_target, stop_rm_cmd],
            check=True,
            stdout=subprocess.DEVNULL, # Suppress remote docker output
            stderr=subprocess.PIPE     # Capture errors if they happen
        )
        print(f"   ✅ Successfully deleted {name}")
        
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr.decode().strip()
        print(f"   🚨 Failed to delete {name}. Error: {error_msg}")

print("-" * 50)
print("✅ Global Cleanup Complete.")