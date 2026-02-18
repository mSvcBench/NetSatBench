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
    pass

class RemoteCommandError(RuntimeError):
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

def get_node_info(etcd_client, node_name):
    cfg_val, _ = etcd_client.get(f"/config/nodes/{node_name}")
    cfg = None
    if cfg_val:
        try:
            cfg = json.loads(cfg_val.decode())
            # check status on worker if possible
            status = "unknown"
            if "worker" in cfg:
                worker_info = etcd_client.get(f"/config/workers/{cfg['worker']}")[0]
                if worker_info:
                    worker_info = json.loads(worker_info.decode())
                    running_nodes = running_containers_on_worker(
                        ssh_username=worker_info.get("ssh-user", "ubuntu"),
                        ssh_key_path=worker_info.get("ssh-key", "~/.ssh/id_rsa"),
                        ssh_host=worker_info.get("ip"),
                    )
                    if node_name in running_nodes:
                        status = "running"
                    else:
                        status = "not running" 
            else:
                status = "not scheduled"
            cfg["status"] = status
        except Exception:
            pass
    links = []
    for link_val, _ in etcd_client.get_prefix(f"/config/links/{node_name}/"):
        try:
            link_cfg = json.loads(link_val.decode())
            links.append(link_cfg)
        except Exception:
            pass
    return cfg, links


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
    parser.add_argument("-v", "--verbose", action="store_true", help="Show all node details")
    
    parser.add_argument(
        "node",
        help="Target node name"
    )

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

    # ==========================================
    # RETRIEVE NODE INFO
    # ==========================================
    node_cfg,node_links = get_node_info(etcd_client, args.node)
    if not node_cfg:
        log.error(f"❌ Node '{args.node}' not found in Etcd")
        sys.exit(1)

    if node_cfg.get("type") == "satellite":
        prefix = "🛰️"
    elif node_cfg.get("type") == "user":
        prefix = "👤"
    elif node_cfg.get("type") == "gateway":
        prefix = "📡"
    else:
        prefix = "🛰️"
    

    print(f"{prefix} Target node='{args.node}'")
    print(f"   - Status: {node_cfg.get('status', 'unknown')}")
    print(f"   - Type: {node_cfg.get('type', 'unknown')}")
    print(f"   - Links: {len(node_links)}")
    for link in node_links:
        if link.get("endpoint1") == args.node:
            link["remote_node"] = link.get("endpoint2")
        elif link.get("endpoint2") == args.node:
            link["remote_node"] = link.get("endpoint1")
        print(f"      - To {link.get('remote_node', 'unknown')} rate {link.get('rate', 'unknown')} loss {link.get('loss', 'unknown')} delay {link.get('delay', 'unknown')}")
    print(f"   - Worker: {node_cfg.get('worker', 'unknown')}")
    print(f"   - CIDR-v4: {node_cfg.get('L3-config', {}).get('cidr', 'unknown')}")
    print(f"   - CIDR-v6: {node_cfg.get('L3-config', {}).get('cidr-v6', 'unknown')}")
    if args.verbose:
        print (f"   - Resource requests: CPU {node_cfg.get('cpu-request', 'unknown')} MEM {node_cfg.get('mem-request', 'unknown')}")
        print (f"   - Resource limits: CPU {node_cfg.get('cpu-limit', 'unknown')} MEM {node_cfg.get('mem-limit', 'unknown')}")
        print (f"   - Full L3 config: {json.dumps(node_cfg.get('L3-config', {}), indent=4)}")
        print (f"   - Metadata: {json.dumps(node_cfg.get('metadata', {}), indent=4)}")
        print (f"   - Image: {node_cfg.get('image', 'unknown')}")
        print (f"   - Sidecars: {json.dumps(node_cfg.get('sidecars', []), indent=4)}")
        print (f"   - Eth0 IP: {node_cfg.get('eth0_ip', 'unknown')}")
        
if __name__ == "__main__":
    main()
