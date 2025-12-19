#!/usr/bin/env python3
import etcd3
import subprocess
import json
import os
import sys

# ==========================================
# 🚩 CONFIGURATION
# ==========================================
# get ETCD_HOST, ETCD_PORT and SAT_HOST_BRIDGE_NAME from environment variables if set
ETCD_HOST = os.getenv('ETCD_HOST', '127.0.0.1')
ETCD_PORT = int(os.getenv('ETCD_PORT', 2379))

try:
    etcd = etcd3.client(host=ETCD_HOST, port=ETCD_PORT)
except Exception as e:
    print(f"❌ Failed to initialize Etcd client: {e}")
    sys.exit(1)

# ==========================================
# HELPERS
# ==========================================
def get_prefix_data(prefix) -> dict:
    data = {}
    for value, metadata in etcd.get_prefix(prefix):
        key = metadata.key.decode('utf-8').split('/')[-1]
        try:
            data[key] = json.loads(value.decode('utf-8'))
        except json.JSONDecodeError:
            print(f"⚠️ Warning: Could not parse JSON for key {key}")
    return data


def run(cmd: str) -> subprocess.CompletedProcess:
    """
    Run a shell command and return the CompletedProcess.
    Uses bash so you can pass a full command string.
    """
    return subprocess.run(
        cmd,
        shell=True,
        executable="/bin/bash",
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

# ==========================================
# INJECT CONFIGURATION IN ETCD    
# ==========================================
## load json from file host-config.json and apply to Etcd

filename = os.path.basename("config.json")
try:
    # Open in r+ mode to allow reading AND writing back to the same file
    with open(filename, "r+", encoding="utf-8") as f:
        config = json.load(f)
        file_modified = False

        # --- 3. ETCD SYNC ---
        allowed_keys = ["L3-config", "hosts", "epoch-config","satellites", "users", "grounds"]

        # A. Push General Config & Inventory
        for key, value in config.items():
            if key not in allowed_keys:
                # the key should not be present in epoch file, skip it
                print(f"❌ [{filename}] Unexpected key '{key}' found in epoch file, skipping...")
                continue
            if key == "L3-config":
                for k, v in value.items():
                    etcd.put(f"/config/L3-config/{k}", str(v).strip().replace('"', ''))
            elif key in ["epoch-config"]:
                    etcd.put(f"/config/{key}", json.dumps(value))
            elif key in ["hosts", "satellites", "users", "grounds"]:
                for k, v in value.items():
                    etcd.put(f"/config/{key}/{k}", json.dumps(v))

    print(f"✅ Successfully applied {filename} to Etcd.")
except FileNotFoundError:
    print(f"❌ Error: File {filename} not found.")
    sys.exit(1)
except json.JSONDecodeError as e:
    print(f"❌ Error: Failed to parse JSON in {filename}: {e}")
    sys.exit(1)

# ==========================================
# CONFIGURE HOSTS    
# ==========================================
## read hosts from /config/hosts/
hosts = get_prefix_data('/config/hosts/')
for host_name, host in hosts.items():
    print(f"➞ Configuring host: {host_name}")
    # Here you can add any host-specific configuration logic if needed
    # For now, we just print the host info
    print(f"    Host Info: {host}")
    ssh_user = host.get('ssh_user', 'ubuntu')
    ssh_ip = host.get('ip', host_name)
    ssh_key = host.get('ssh_key', '~/.ssh/id_rsa')
    # Example: You could run a remote command to verify connectivity
    try:
        subprocess.run(f"ssh -o StrictHostKeyChecking=no -i {ssh_key} {ssh_user}@{ssh_ip} 'echo Host {host_name} is reachable'", 
                       shell=True, check=True)
    except subprocess.CalledProcessError as e:
        print(f"❌ Failed to connect to host {host_name} at {ssh_ip}: {e}")

    sat_vnet_cidr = host.get('sat-vnet-cidr', None)
    sat_vnet = host.get('sat-vnet', 'sat-vnet')
    host_ip = host.get('ip', host_name)

    # === Create or verify Docker network remotely ===
    print(f"🌍 Target host: {host_ip}  →  Configuring Docker Network {sat_vnet} with Subnet {sat_vnet_cidr}")
    remote = f"{ssh_user}@{host_ip} -i {ssh_key}"

    inspect_cmd = f"ssh {remote} docker network inspect {sat_vnet}"
    inspect = run(inspect_cmd)

    if inspect.returncode == 0:
        print(f"✔️  Docker network '{sat_vnet}' already exists on {host_ip}.")
    else:
        print(f"🧱 Creating Docker network '{sat_vnet}' on {host_ip} ...")
        create_cmd = (
            f"ssh {remote} docker network create --driver=bridge"
            f" --subnet={sat_vnet_cidr}"
            f" -o com.docker.network.bridge.enable_ip_masquerade=false {sat_vnet}"
        )
        
        created = run(create_cmd)
        if created.returncode != 0:
            raise RuntimeError(
                "Failed to create remote docker network.\n"
                f"CMD: {create_cmd}\n"
                f"STDOUT:\n{created.stdout}\n"
                f"STDERR:\n{created.stderr}"
            )

        print(f"✅ Docker network '{sat_vnet}' created successfully on {host_ip}.")

# ==========================================
# CONFIGURE ALL TO ALL ROUTES AMONG SAT-VNET
# ==========================================
for host_name, host in hosts.items():
    print(f"➞ Configuring routes on host: {host_name}")
    ssh_user = host.get('ssh_user', 'ubuntu')
    ssh_ip = host.get('ip', host_name)
    ssh_key = host.get('ssh_key', '~/.ssh/id_rsa')
    remote = f"{ssh_user}@{ssh_ip} -i {ssh_key}"
    sat_vnet = host.get('sat-vnet', 'sat-vnet')

    for other_host_name, other_host in hosts.items():
        if other_host_name == host_name:
            continue  # Skip self
        other_host_ip = other_host.get('ip', other_host_name)
        other_sat_vnet_cidr = other_host.get('sat-vnet-cidr', None)
        if not other_sat_vnet_cidr:
            print(f"⚠️  Skipping route to {other_host_name}: No sat-vnet-cidr defined.")
            continue

        print(f"   ➞ Adding route to {other_host_name} ({other_sat_vnet_cidr}) via {other_host_ip} ...")
        route_cmd = (
            f"ssh {remote} sudo ip route replace {other_sat_vnet_cidr} via {other_host_ip}"
        )
        routed = run(route_cmd)
        if routed.returncode != 0:
            print(f"❌ Failed to add route to {other_host_name} on {host_name}.\n"
                  f"CMD: {route_cmd}\n"
                  f"STDOUT:\n{routed.stdout}\n"
                  f"STDERR:\n{routed.stderr}")
        else:
            print(f"✅ Route to {other_host_name} added successfully on {host_name}.")

