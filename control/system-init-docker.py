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
etcd_client = None

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
        if target_ip in line:
            return line.split()[1]

    return None

def get_prefix_data(prefix) -> dict:
    data = {}
    for value, metadata in etcd_client.get_prefix(prefix):
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
    global etcd_client

    parser = argparse.ArgumentParser(
        description="Configure workers of the emulation"
    )
    parser.add_argument(
        "-c", "--config",
        default="worker-config.json",
        required=False,
        help="Path to the JSON worker configuration file (e.g., worker-config.json)",
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
    parser.add_argument(
        "--etcd-user",
        default=os.getenv("ETCD_USER", None ),
        help="Etcd user (default: env ETCD_USER or None)",
    )
    parser.add_argument(
        "--etcd-password",
        default=os.getenv("ETCD_PASSWORD", None ),
        help="Etcd password (default: env ETCD_PASSWORD or None)",
    )
    parser.add_argument(
        "--etcd-ca-cert",
        default=os.getenv("ETCD_CA_CERT", None ),
        help="Path to Etcd CA certificate (default: env ETCD_CA_CERT or None)",
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
        if args.etcd_user and args.etcd_password and args.etcd_ca_cert:
            etcd_client = etcd3.client(host=args.etcd_host, port=args.etcd_port, user=args.etcd_user, password=args.etcd_password, ca_cert=args.etcd_ca_cert)
        else:
            etcd_client = etcd3.client(host=args.etcd_host, port=args.etcd_port)
    except Exception as e:
        print(f"❌ Failed to initialize Etcd client: {e}")
        sys.exit(1)
    
    # ==========================================
    # INJECT CONFIGURATION IN ETCD    
    # ==========================================
    ## load json from file config.json and apply to Etcd

    allowed_keys = ["satellites", "users", "grounds", "L3-config-common", "workers", "epoch-config"]

    # A. Push Worker Config to Etcd
    for key, value in config.items():
        if key not in allowed_keys:
            # the key should not be present in epoch file, skip it
            print(f"❌ [{args.config}] Unexpected key '{key}' found in epoch file, skipping...")
            continue
        elif key in ["workers"]:
            for k, v in value.items():
                etcd_client.put(f"/config/{key}/{k}", json.dumps(v))


    # ==========================================
    # CONFIGURE WORKERS    
    # ==========================================

    workers = get_prefix_data('/config/workers/')
    for worker_name, worker in workers.items():
        # print(f"➞ Configuring worker: {worker_name}")
        # Here you can add any host-specific configuration logic if needed
        # For now, we just print the host info
        # print(f"    Host Info: {worker}")
        ssh_user = worker.get('ssh_user', 'ubuntu')
        ssh_ip = worker.get('ip', worker_name)
        ssh_key = worker.get('ssh_key', '~/.ssh/id_rsa')
        ssh_interface_name = interface_from_ip_ssh(ssh_user, ssh_ip, ssh_key, worker.get('ip', worker_name))
        sat_vnet_cidr = worker.get('sat-vnet-cidr', None)
        sat_vnet = worker.get('sat-vnet', 'sat-vnet')
        worker_ip = worker.get('ip', worker_name)
        remote_str = f"{ssh_user}@{worker_ip} -i {ssh_key}"
        
        print(f"🖥️ Configuring worker {worker_name} at {worker_ip}")
       
        # Verify connectivity
        try:
            subprocess.run(f"ssh -o StrictHostKeyChecking=no -i {ssh_key} {ssh_user}@{ssh_ip} 'echo > /dev/null'", 
                        shell=True, check=True)
        except subprocess.CalledProcessError as e:
            print(f"    ❌ Failed to connect to worker {worker_name} at {ssh_ip}: {e}")

        # === Create or verify Docker network remotely ===
        inspect_cmd = f"ssh {remote_str} docker network inspect {sat_vnet}"
        inspect = run(inspect_cmd)
        if inspect.returncode == 0:
            # print(f"✔️  Docker network '{sat_vnet}' already exists on {worker_ip}, remove it.")
            remove_cmd = f"ssh {remote_str} docker network rm {sat_vnet}"
            removed = run(remove_cmd)
            if removed.returncode != 0:
                raise RuntimeError(
                    "Failed to remove existing remote docker network.\n"
                    f"CMD: {remove_cmd}\n"
                    f"STDOUT:\n{removed.stdout}\n"
                    f"STDERR:\n{removed.stderr}"
                )
        create_cmd = (
            f"ssh {remote_str} docker network create --driver=bridge"
            f" --subnet={sat_vnet_cidr}"
            f" -o com.docker.network.bridge.enable_ip_masquerade=false"
            f" -o com.docker.network.bridge.trusted_host_interfaces=\"{ssh_interface_name}\""
            f" {sat_vnet}"
        )
        
        created = run(create_cmd)
        if created.returncode != 0:
            raise RuntimeError(
                "   ❌ Failed to create remote docker network.\n"
                f"CMD: {create_cmd}\n"
                f"STDOUT:\n{created.stdout}\n"
                f"STDERR:\n{created.stderr}"
            )
        print(f"    ✅ Docker network '{sat_vnet}' created successfully.")

        # Add DOCKER-USER iptables rule to allow forwarding between local and remote containers
        sat_vnet_supercidr = worker.get('sat-vnet-supernet', '172.0.0.0/8')
        check_forward_cmd = (
                f"ssh {remote_str} sudo iptables -C DOCKER-USER -s {sat_vnet_supercidr}"
                f" -d {sat_vnet_supercidr}"
                f" -j ACCEPT"
        )
        check_forwarded = run(check_forward_cmd)
        if check_forwarded.returncode == 0:
            print(f"    ✅ DOCKER-USER iptables rule enabled successfully.")
        else:    
            forward_cmd = (
                f"ssh {remote_str} sudo iptables -I DOCKER-USER -s {sat_vnet_supercidr}"
                f" -d {sat_vnet_supercidr}"
                f" -j ACCEPT"
            )
            forwarded = run(forward_cmd)
            if forwarded.returncode != 0:
                print(f"    ❌ Failed to enable DOCKER-USER iptables rule."
                    f"CMD: {forward_cmd}\n"
                    f"STDOUT:\n{forwarded.stdout}\n"
                    f"STDERR:\n{forwarded.stderr}")
            else:
                print(f"    ✅ DOCKER-USER iptables rule enabled successfully.")

        
        for other_worker_name, other_worker in workers.items():
            if other_worker_name == worker_name:
                continue  # Skip self
            other_worker_ip = other_worker.get('ip', other_worker_name)
            other_sat_vnet_cidr = other_worker.get('sat-vnet-cidr', None)
            if not other_sat_vnet_cidr:
                print(f"    ⚠️  Skipping route to {other_worker_name}: No sat-vnet-cidr defined.")
                continue
            route_cmd = (
                f"ssh {remote_str} sudo ip route replace {other_sat_vnet_cidr} via {other_worker_ip}"
            )
            routed = run(route_cmd)
            if routed.returncode != 0:
                print(f"    ❌ Failed to add route to {other_worker_name}."
                    f"CMD: {route_cmd}\n"
                    f"STDOUT:\n{routed.stdout}\n"
                    f"STDERR:\n{routed.stderr}")
            else:
                print(f"    ✅ IP route to {other_worker_name} added successfully")
    print("👍 All workers configured successfully.")
        

if __name__ == "__main__":
    main()
