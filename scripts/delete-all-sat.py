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
            pass
    return data

# ==========================================
# 1. LOAD CONFIGURATION
# ==========================================
print(f"📡 Connecting to Etcd at {ETCD_HOST}:{ETCD_PORT}...")

# Fetch Satellites AND Users to clean up both
satellites = get_prefix_data('/config/satellites/')
users = get_prefix_data('/config/users/')
hosts = get_prefix_data('/config/hosts/')

# Merge for processing
all_nodes = {**satellites, **users}

print(f"   Found {len(satellites)} satellites and {len(users)} users to clean up.")

if not all_nodes:
    print("⚠️  No nodes found in Etcd configuration.")

# ==========================================
# 2. DELETE CONTAINERS
# ==========================================
print("🧹 Starting cleanup...")

for name, node in all_nodes.items():
    host_id = node.get('host')
    
    if host_id not in hosts:
        print(f"⚠️  Skipping {name}: Unknown host '{host_id}'")
        continue

    host_info = hosts[host_id]
    ssh_user = host_info.get('ssh_user', 'ubuntu')
    
    # Use host ID as the SSH hostname (relies on ~/.ssh/config or DNS/hosts file)
    ssh_host = host_id 

    print(f"🗑️  Deleting node: {name} from {ssh_host}")

    # Check if container exists
    check_cmd = [
        'ssh', f"{ssh_user}@{ssh_host}",
        f"docker ps -a --format '{{{{.Names}}}}' | grep -Fxq '{name}'"
    ]
    
    # We use subprocess.run to check exit code (0 = found, 1 = not found)
    result = subprocess.run(check_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    if result.returncode != 0:
        print(f"   ❌ Container '{name}' does not exist on {ssh_host}")
        continue

    # Stop and Remove
    try:
        subprocess.run([
            'ssh', f"{ssh_user}@{ssh_host}",
            f"docker stop {name} && docker rm {name}"
        ], check=True, stdout=subprocess.DEVNULL)
        print(f"   ✅ Deleted {name}")
    except subprocess.CalledProcessError as e:
        print(f"   ❌ Failed to delete {name}: {e}")

print("\n✅ Cleanup Complete.")