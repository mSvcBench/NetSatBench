#!/usr/bin/env python3
import argparse
import etcd3
import subprocess
import json
import os
import sys
import re

# ==========================================
# 🚩 CONFIGURATION
# ==========================================
etcd = None

# ==========================================
# HELPERS
# ==========================================

def interface_from_ip_ssh(ssh_user, ssh_ip, ssh_key, target_ip):
    cmd = (
        f"ssh -o StrictHostKeyChecking=no "
        f"-i {ssh_key} "
        f"{ssh_user}@{ssh_ip} "
        f"'ip -o -4 addr show'"
    )

    result = subprocess.run(
        cmd,
        shell=True,
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())

    for line in result.stdout.splitlines():
        # Example:
        # 2: eth0    inet 192.168.1.10/24 brd ...
        if target_ip in line:
            return line.split()[1]

    return None

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

def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

# ==========================================
# Main Init Logic
# ==========================================

def main():
    global etcd

    parser = argparse.ArgumentParser(
        description="Configure hosts of the constellation"
    )
    parser.add_argument(
        "-c", "--config",
        required=True,
        help="Path to the JSON configuration file (e.g., config.json)",
    )
    parser.add_argument(
        "--etcd-host",
        default=os.getenv("ETCD_HOST", "127.0.0.1"),
        help="Etcd host (default: env ETCD_HOST or 127.0.0.1)",
    )
    parser.add_argument(
        "--etcd-port",
        type=int,
        default=int(os.getenv("ETCD_PORT", 2379)),
        help="Etcd port (default: env ETCD_PORT or 2379)",
    )

    args = parser.parse_args()
    
    
    try:
        config = load_json(args.config)
    except FileNotFoundError:
        print(f"❌ Error: File {args.config} not found.")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"❌ Error: Failed to parse JSON in {args.config}: {e}")
        sys.exit(1)
    
    try:
        etcd = etcd3.client(host=ETCD_HOST, port=ETCD_PORT)
    except Exception as e:
        print(f"❌ Failed to initialize Etcd client: {e}")
        sys.exit(1)
    
    # ==========================================
    # INJECT CONFIGURATION IN ETCD    
    # ==========================================
    ## load json from file host-config.json and apply to Etcd

    allowed_keys = ["satellites", "users", "grounds", "L3-config", "hosts", "epoch-config"]

    # A. Push General Config & Inventory
    for key, value in config.items():
        if key not in allowed_keys:
            # the key should not be present in epoch file, skip it
            print(f"❌ [{args.config}] Unexpected key '{key}' found in epoch file, skipping...")
            continue
        elif key in ["hosts"]:
            for k, v in value.items():
                etcd.put(f"/config/{key}/{k}", json.dumps(v))


    # ==========================================
    # CONFIGURE HOSTS    
    # ==========================================

    hosts = get_prefix_data('/config/hosts/')
    for host_name, host in hosts.items():
        print(f"➞ Configuring host: {host_name}")
        # Here you can add any host-specific configuration logic if needed
        # For now, we just print the host info
        print(f"    Host Info: {host}")
        ssh_user = host.get('ssh_user', 'ubuntu')
        ssh_ip = host.get('ip', host_name)
        ssh_key = host.get('ssh_key', '~/.ssh/id_rsa')
        ssh_interface_name = interface_from_ip_ssh(ssh_user, ssh_ip, ssh_key, host.get('ip', host_name))

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
            print(f"✔️  Docker network '{sat_vnet}' already exists on {host_ip}, remove it.")
            remove_cmd = f"ssh {remote} docker network rm {sat_vnet}"
            removed = run(remove_cmd)
            if removed.returncode != 0:
                raise RuntimeError(
                    "Failed to remove existing remote docker network.\n"
                    f"CMD: {remove_cmd}\n"
                    f"STDOUT:\n{removed.stdout}\n"
                    f"STDERR:\n{removed.stderr}"
                )
        print(f"🧱 Creating Docker network '{sat_vnet}' on {host_ip} ...")
        create_cmd = (
            f"ssh {remote} docker network create --driver=bridge"
            f" --subnet={sat_vnet_cidr}"
            f" -o com.docker.network.bridge.enable_ip_masquerade=false"
            f" -o com.docker.network.bridge.trusted_host_interfaces=\"{ssh_interface_name}\""
            f" {sat_vnet}"
        )
        
        created = run(create_cmd)
        if created.returncode != 0:
            raise RuntimeError(
                "Failed to create remote docker network.\n"
                f"CMD: {create_cmd}\n"
                f"STDOUT:\n{created.stdout}\n"
                f"STDERR:\n{created.stderr}"
            )
        
        # === enable container to container input forwarding among hosts === 
        sat_vnet_supercidr = host.get('SAT-VNET-SUPERNET', '172.0.0.0/8')
        print(f"➞ Enabling container-to-container forwarding on host: {host_name}")
        # Add iptables rule to allow forwarding from {host_name2} to {host_name}
        check_forward_cmd = (
                f"ssh {remote} sudo iptables -C DOCKER-USER -s {sat_vnet_supercidr}"
                f" -d {sat_vnet_supercidr}"
                f" -j ACCEPT"
        )
        check_forwarded = run(check_forward_cmd)
        if check_forwarded.returncode == 0:
            print(f"✅ Container-to-container forwarding on {host_name} already enabled, skipping.")
        else:    
            forward_cmd = (
                f"ssh {remote} sudo iptables -I DOCKER-USER -s {sat_vnet_supercidr}"
                f" -d {sat_vnet_supercidr}"
                f" -j ACCEPT"
            )
            forwarded = run(forward_cmd)
            if forwarded.returncode != 0:
                print(f"❌ Failed to enable container-to-container forwarding on {host_name}.\n"
                    f"CMD: {forward_cmd}\n"
                    f"STDOUT:\n{forwarded.stdout}\n"
                    f"STDERR:\n{forwarded.stderr}")
            else:
                print(f"✅ Container-to-container forwarding enabled successfully on {host_name}.")

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

if __name__ == "__main__":
    main()
