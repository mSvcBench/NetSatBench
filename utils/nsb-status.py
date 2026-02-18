#!/usr/bin/env python3
# ==========================================
# MAIN
# ==========================================
import argparse
import json
import logging
import os
import sys
import subprocess
import etcd3

logging.basicConfig(level="INFO", format="[%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ==========================================
# 🚩 CONFIGURATION
# ==========================================
etcd_client = None

# ==========================================
# HELPERS
# ==========================================

class SshError(RuntimeError):
    log.error("SSH error: %s", str(RuntimeError))   
    pass

class RemoteCommandError(RuntimeError):
    log.error("Remote command error: %s", str(RuntimeError))
    pass

def run_ssh(
    *,
    ssh_username: str,
    ssh_host: str,
    ssh_key_path: str,
    remote_args: list[str],
    check: bool = False,
    quiet: bool = False,
    timeout: int = 30,
) -> subprocess.CompletedProcess:
    """
    Run: ssh -i <key> user@host <remote_args...>

    - Raises SshError on SSH transport problems
    - Raises RemoteCommandError if check=True and remote command fails
    - Error messages are concise (only stderr summary)
    """
    cmd = [
        "ssh",
        "-i", ssh_key_path,
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no",
        "-o", f"ConnectTimeout={timeout}",
        f"{ssh_username}@{ssh_host}",
        "--",
        *remote_args,
    ]
    
    try:
        cp = subprocess.run(
            cmd,
            text=True,
            stdout=(subprocess.DEVNULL if quiet else subprocess.PIPE),
            stderr=(subprocess.DEVNULL if quiet else subprocess.PIPE),
            timeout=timeout + 5,
        )
    except subprocess.TimeoutExpired:
        raise SshError(f"SSH timeout connecting to {ssh_username}@{ssh_host}")

    stderr = (cp.stderr or "").strip()

    # SSH transport failures are typically exit code 255
    if cp.returncode == 255:
        msg = stderr.splitlines()[0] if stderr else "SSH transport error"
        raise SshError(msg)

    if check and cp.returncode != 0:
        msg = stderr.splitlines()[0] if stderr else "Remote command failed"
        log.error(f"❌ {msg}")
        raise RemoteCommandError(msg)

    return cp

def running_containers_on_worker(
    ssh_username: str,
    ssh_key_path: str,
    ssh_host: str,
) -> list[str]:

    try:
        # --- Check if container exists ---
        ps = run_ssh(
            ssh_username=ssh_username,
            ssh_host=ssh_host,
            ssh_key_path=ssh_key_path,
            remote_args=["docker", "ps", "-a", "--format", "{{.Names}}"],
            check=True,  # if docker command fails, raise RemoteCommandError
        )
        names = ps.stdout.splitlines() if ps.stdout else []
        return names
    except SshError as e:
        log.error(f"❌ SSH error while checking container on worker {ssh_host}: {e}")
        return []
    except RemoteCommandError as e:
        log.error(f"❌ Remote command error while checking container on worker {ssh_host}: {e}")
        return []

def get_node_info(etcd_client, worker_dict, verbose=False):
    node_dict = {}
    if verbose:
        running_nodes={}
        for worker_name, worker_info in worker_dict.items():
            running_nodes[worker_name] = running_containers_on_worker(
                                ssh_host=worker_info.get("ip", ""),
                                ssh_username=worker_info.get("ssh-user", "ubuntu"),
                                ssh_key_path=worker_info.get("ssh-key", "~/.ssh/id_rsa"),
                            )
    print(running_nodes) if verbose else None
    for node_val, node_key in etcd_client.get_prefix(f"/config/nodes/"):
        try:
            status = "unknown"
            node_cfg = json.loads(node_val.decode())
            node_cfg["name"] = node_key.key.decode().split("/")[-1]
            if verbose:
                if not node_cfg.get("worker"):
                    status = "not scheduled"
                elif node_cfg["worker"] not in worker_dict:
                    status = "worker not found"
                # if worker is found, we can check if the container for the node is running on
                elif node_cfg["name"] in running_nodes[node_cfg["worker"]]:                    
                        status = "running"
                else:
                    status = "not running"
            node_cfg["status"] = status
            node_dict[node_cfg["name"]] = node_cfg
        except Exception as e:
            log.warning(f"❌ Failed to process node config {node_cfg.get('name', 'unknown')}: {e}")
    return node_dict

def get_link_info(etcd_client):
    link_list = []
    for link_val, _ in etcd_client.get_prefix(f"/config/links/"):
        try:
            link_cfg = json.loads(link_val.decode())
            link_list.append(link_cfg)
        except Exception:
            pass
    return link_list

def get_worker_info(etcd_client):
    worker_dict = {}
    for worker_val, worker_key in etcd_client.get_prefix(f"/config/workers/"):
        try:
            worker_cfg = json.loads(worker_val.decode())
            worker_name = worker_key.key.decode().split("/")[-1]
            worker_dict[worker_name] = worker_cfg
        except Exception:
            pass
    return worker_dict

def get_epoch_config(etcd_client):
    cfg_val, _ = etcd_client.get(f"/config/epoch-config")
    cfg = None
    if cfg_val:
        try:
            cfg = json.loads(cfg_val.decode())
        except Exception:
            pass
    return cfg

# ==========================================
# MAIN
# ==========================================

def main():
    global etcd_client

    parser = argparse.ArgumentParser(
        description="Dump information about a node"
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
        default=os.getenv("ETCD_USER", None),
        help="Etcd user (default: env ETCD_USER or None)",
    )
    parser.add_argument(
        "--etcd-password",
        default=os.getenv("ETCD_PASSWORD", None),
        help="Etcd password (default: env ETCD_PASSWORD or None)",
    )
    parser.add_argument(
        "--etcd-ca-cert",
        default=os.getenv("ETCD_CA_CERT", None),
        help="Path to Etcd CA certificate (default: env ETCD_CA_CERT or None)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (default: INFO)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Show all worker details")
    
    args = parser.parse_args()

    log.setLevel(args.log_level.upper())
    # ==========================================
    # INIT ETCD
    # ==========================================
    try:
        if args.etcd_user and args.etcd_password and args.etcd_ca_cert:
            etcd_client = etcd3.client(
                host=args.etcd_host,
                port=args.etcd_port,
                user=args.etcd_user,
                password=args.etcd_password,
                ca_cert=args.etcd_ca_cert,
            )
        else:
            etcd_client = etcd3.client(host=args.etcd_host, port=args.etcd_port)
    except Exception as e:
        log.error(f"❌ Failed to initialize Etcd client: {e}")
        sys.exit(1)


    print(f"🛰️ NetSatBench System Status") if not args.verbose else print(f"🛰️ NetSatBench System Status -- Verbose")
    print(f"===========================")
    print("⏳ Gathering worker information...")
    worker_dict = get_worker_info(etcd_client=etcd_client)
    print("⏳ Gathering node information...") if not args.verbose else print("⏳ Gathering node information...(verbose mode: checking container status on workers, this may take a while...)")
    node_dict = get_node_info(etcd_client=etcd_client, worker_dict=worker_dict, verbose=args.verbose)
    print("⏳ Gathering link information...")
    link_list = get_link_info(etcd_client=etcd_client)
    print("⏳ Gathering epoch configuration...")
    epoch_cfg = get_epoch_config(etcd_client=etcd_client)
    print(f"===========================")
    print(f" Workers: {len(worker_dict)}")
    if args.verbose:
        for worker_name, worker in worker_dict.items():
            print(f"   - {worker_name}:")
            print(f"      - CPU: {worker.get('cpu', 'unknown')}")
            print(f"      - MEM: {worker.get('mem', 'unknown')}")
            print(f"      - CPU used: {worker.get('cpu-used', 'unknown')}")
            print(f"      - MEM used: {worker.get('mem-used', 'unknown')}")
            node_count = 0;
            for _, node in node_dict.items():
                if node.get("worker") == worker_name:
                    node_count += 1
            print(f"      - Nodes: {node_count}")
        
    print(f"===========================")
    print(f" Nodes: {len(node_dict)}") 
    n_satellites = sum(1 for node in node_dict.values() if node.get("type") == "satellite")
    n_users = sum(1 for node in node_dict.values() if node.get("type") == "user")
    n_gateways = sum(1 for node in node_dict.values() if node.get("type") == "gateway")
    n_satellites_running = sum(1 for node in node_dict.values() if node.get("type") == "satellite" and node.get("status") == "running")
    n_users_running = sum(1 for node in node_dict.values() if node.get("type") == "user" and node.get("status") == "running")
    n_gateways_running = sum(1 for node in node_dict.values() if node.get("type") == "gateway" and node.get("status") == "running")
    n_others_running = sum(1 for node in node_dict.values() if node.get("type") not in ["satellite", "user", "gateway"] and node.get("status") == "running")
    others = sum(1 for node in node_dict.values() if node.get("type") not in ["satellite", "user", "gateway"])
    if args.verbose:
        print(f"    🛰️ Satellites: {n_satellites} ({n_satellites_running} running)")
        print(f"    👤 Users: {n_users} ({n_users_running} running)")
        print(f"    📡 Gateways: {n_gateways} ({n_gateways_running} running)") 
        print(f"    📦 Others: {others} ({n_others_running} running)")

        for node_key, node in node_dict.items():
            if node.get("status")!="running":
                print(f"    ⚠️ Node {node_key} has status '{node.get('status')}'")

    else:
        print(f"    🛰️ Satellites: {n_satellites}")
        print(f"    👤 Users: {n_users}")
        print(f"    📡 Gateways: {n_gateways}") 
        print(f"    📦 Others: {others}")
    
    
    print(f"===========================")
    print(f" Links: {len(link_list)}")
    
    print(f"===========================")
    print(f" Epoch configuration:")
    if epoch_cfg:
        print(f"   - Epoch dir: {epoch_cfg.get('epoch-dir', 'unknown')}")
        print(f"   - File pattern: {epoch_cfg.get('file-pattern', 'unknown')}")
        print(f"   - Epoch file: {epoch_cfg.get('epoch-file', 'emulation not started')}")
        print(f"   - Epoch time: {epoch_cfg.get('epoch-time', 'emulation not started')}")
    print(f"===========================")
        
if __name__ == "__main__":
    main()
