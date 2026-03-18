#!/usr/bin/env python3
import argparse
import concurrent.futures
import logging
import time
from astropy.units import MiB
import etcd3
import subprocess
import json
import os
import sys
from typing import Dict, Any, Tuple
from scheduler import parse_cpu, parse_mem


logging.basicConfig(level="INFO", format="[%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ==========================================
# HELPERS
# ==========================================
def connect_etcd(etcd_host: str, etcd_port: int, etcd_user = None, etcd_password = None, etcd_ca_cert = None):
    try:
        log.info(f"📁 Connecting to Etcd at {etcd_host}:{etcd_port}...")
        if etcd_user and etcd_password:
            client = etcd3.client(host=etcd_host, port=etcd_port, user=etcd_user, password=etcd_password, ca_cert=etcd_ca_cert)
            client.status()  # Test connection, if fail will raise
            return client
        else:
            client = etcd3.client(host=etcd_host, port=etcd_port)
            client.status()  # Test connection, if fail will raise
            return client
    except Exception as e:
        log.error(f"❌ Failed to initialize Etcd client: {e}")
        sys.exit(1)

def get_prefix_data(etcd, prefix: str) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for value, metadata in etcd.get_prefix(prefix):
        key = metadata.key.decode('utf-8').split('/')[-1]
        try:
            data[key] = json.loads(value.decode('utf-8'))
        except json.JSONDecodeError:
            log.warning(f"⚠️ Warning: Could not parse JSON for key {key} under {prefix}")
    return data

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

def recreate_and_run_container(
    *,
    node_name: str,
    worker: str,
    ssh_username: str,
    ssh_key_path: str,
    worker_bridge: str,
    container_image: str,
    cpu_requested: float,
    mem_requested: str,
    cpu_limit: float,
    mem_limit: str,
    etcd_host: str,
    etcd_port: int,
    etcd_user: str = None,
    etcd_password: str = None,
    etcd_ca_cert: str = None,
) -> None:

    try:
        # --- Check if container exists ---
        ps = run_ssh(
            ssh_username=ssh_username,
            ssh_host=worker,
            ssh_key_path=ssh_key_path,
            remote_args=["docker", "ps", "-a", "--format", "{{.Names}}"],
            check=True,  # if docker command fails, raise RemoteCommandError
        )

        names = ps.stdout.splitlines() if ps.stdout else []
        exists = node_name in names

        # --- Remove existing container (ignore errors, but still raise SSH transport errors) ---
        if exists:
            run_ssh(
                ssh_username=ssh_username,
                ssh_host=worker,
                ssh_key_path=ssh_key_path,
                remote_args=["docker", "rm", "-f", node_name],
                check=False,   # ignore non-zero from docker rm
                quiet=True,
            )
        # --- Run new container ---
        run_cmd = [
                "docker", "run", "-d",
                "--name", node_name,
                "--hostname", node_name,
                "--net", worker_bridge,
                "--privileged",
                "--pull=always",
                "-e", f"NODE_NAME={node_name}",
                "-e", f"ETCD_ENDPOINT={etcd_host}:{etcd_port}"
        ]
        if etcd_user and etcd_password and etcd_ca_cert:
            run_cmd.extend([
                "-e", f"ETCD_USER={etcd_user}",
                "-e", f"ETCD_PASSWORD={etcd_password}",
                "-e", f"ETCD_CA_CERT=/app/etcd-ca.crt",
            ])
        if cpu_requested > 0:
            run_cmd.extend(["--cpu-shares", str(int(cpu_requested*1024))])  # convert CPU to CPU shares (1024 = 1 CPU)
        else:
            run_cmd.extend(["--cpu-shares", "10"])  # minimal CPU shares such as 0.01 CPU
        if mem_requested != "0MiB":
            run_cmd.extend(["--memory-reservation", str(mem_requested)])
        if cpu_limit > 0:
             run_cmd.extend(["--cpus", str(cpu_limit)])
        if mem_limit != "0MiB":
             run_cmd.extend(["--memory", str(mem_limit)])
        run_cmd.append(container_image) 

        run_ssh(
            ssh_username=ssh_username,
            ssh_host=worker,
            ssh_key_path=ssh_key_path,
            remote_args=run_cmd,
            check=True,
        )

        # copy the CA cert if needed with scp and then and docker cp via ssh
        if etcd_user and etcd_password and etcd_ca_cert:
            scp_cmd = [
                "scp",
                "-i", ssh_key_path,
                "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=no",
                "-o", f"ConnectTimeout=30",
                etcd_ca_cert,
                f"{ssh_username}@{worker}:/tmp/etcd-ca.crt",
            ]
            try:
                cp = subprocess.run(
                    scp_cmd,
                    text=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=35,
                )
            except subprocess.TimeoutExpired:
                raise SshError(f"SCP timeout connecting to {ssh_username}@{worker}")
            
            stderr = (cp.stderr or "").strip()
            if cp.returncode != 0:
                msg = stderr.splitlines()[0] if stderr else "SCP transport error"
                raise SshError(msg)
            
            # now copy into the container
            run_ssh(
                ssh_username=ssh_username,
                ssh_host=worker,
                ssh_key_path=ssh_key_path,
                remote_args=["docker", "cp", "/tmp/etcd-ca.crt", f"{node_name}:/app/etcd-ca.crt"],
                check=True,
            )
    except SshError as e:
        print(f"    ❌ SSH failure: {e}")
        raise RuntimeError({e})
    except RemoteCommandError as e:
        print(f"    ❌ Remote command failed: {e}")
        raise RuntimeError({e})

def create_one_node(
    name: str,
    node: Dict[str, Any],
    workers: Dict[str, Any],
    etcd_host: str,
    etcd_port: int,
    etcd_user: str = None,
    etcd_password: str = None,
    etcd_ca_cert: str = None,    
) -> Tuple[str, bool, str]:
    """
    Returns: (name, success, message)
    """
    worker = node.get('worker', None)
    if not worker:
        return name, False, "❌ Missing 'worker' field in node config"

    if worker not in workers:
        return name, False, f"❌ Unknown worker '{worker}' (node assigned to non-existing /config/workers entry)"

    worker_info = workers[worker]
    worker_ip = worker_info.get('ip', None)
    ssh_user = worker_info.get('ssh-user', 'ubuntu')
    ssh_key = worker_info.get('ssh-key', '~/.ssh/id_rsa')
    worker_bridge = worker_info.get('sat-vnet', 'sat-vnet')
    image = node.get('image', 'msvcbench/sat-container:latest')
    cpu_requested = float(parse_cpu(node.get('cpu-request', 0.0)))
    mem_requested = f"{parse_mem(node.get('mem-request', '0MiB'))*1024}MiB"
    cpu_limit = float(parse_cpu(node.get('cpu-limit', 0.0)))
    mem_limit = f"{parse_mem(node.get('mem-limit', '0MiB'))*1024}MiB"

    try:
        cmd = recreate_and_run_container(
            node_name=name,
            worker=worker_ip,
            ssh_username=ssh_user,
            ssh_key_path=ssh_key,
            worker_bridge=worker_bridge,
            container_image=image,
            cpu_requested=cpu_requested,
            mem_requested=mem_requested,
            cpu_limit=cpu_limit,
            mem_limit=mem_limit,
            etcd_host=etcd_host,
            etcd_port=etcd_port,
            etcd_user=etcd_user,
            etcd_password=etcd_password,
            etcd_ca_cert=etcd_ca_cert,
        )
        msg = f" Restarted on worker={worker}"
        return name, True, msg
    except Exception as e:
        return name, False, f"❌ Deployment failed: {e}"
    

# ==========================================
# MAIN
# ==========================================
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Restart a node of the current emulation"
    )
    parser.add_argument(
        "--etcd-host",
        default=os.getenv("ETCD_HOST", "127.0.0.1"),
        help="Etcd host used by control host (default: env ETCD_HOST or 127.0.0.1)",
    )
    parser.add_argument(
        "--etcd-port",
        type=int,
        default=int(os.getenv("ETCD_PORT", 2379)),
        help="Etcd port used by control host (default: env ETCD_PORT or 2379)",
    )
    parser.add_argument(
        "--node-etcd-host",
        default=os.getenv("NODE_ETCD_HOST", os.getenv("ETCD_HOST", "127.0.0.1")),
        help="Etcd host used by nodes (default: env NODE_ETCD_HOST or env ETCD_HOST or 127.0.0.1)",
    )
    parser.add_argument(
        "--node-etcd-port",
        type=int,
        default=int(os.getenv("NODE_ETCD_PORT", os.getenv("ETCD_PORT", 2379))),
        help="Etcd port used by nodes (default: env NODE_ETCD_PORT or env ETCD_PORT or 2379)",
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
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (default: INFO)",
    )
    parser.add_argument("node", help="Name of the node to restart (must be defined in /config/nodes/ in Etcd)")
    args = parser.parse_args()

    log.setLevel(args.log_level.upper())


    etcd_client = connect_etcd(args.etcd_host, args.etcd_port, args.etcd_user, args.etcd_password, args.etcd_ca_cert)

    try:
        # chacke node existence in etcd
        node_val, _ = etcd_client.get(f"/config/nodes/{args.node}")
        if not node_val:
            log.error(f"❌ Node '{args.node}' not found in Etcd under /config/nodes/. Cannot restart.")
            return 1
    except Exception as e:
        log.error(f"❌ Error checking existing nodes in Etcd: {e}")
        sys.exit(1)
    # extract the worker assigned to the node    node_cfg = json.loads(node_val.decode('utf-8'))
    node_cfg = json.loads(node_val.decode('utf-8'))
    worker_name = node_cfg.get("worker", None)
    if not worker_name:
        log.error(f"❌ Node '{args.node}' does not have a 'worker' field in its Etcd config. Cannot restart.")
        return 1
    worker_val, _ = etcd_client.get(f"/config/workers/{worker_name}")
    if not worker_val:
        log.error(f"❌ Worker '{worker_name}' assigned to node '{args.node}' not found in Etcd under /config/workers/. Cannot restart.")
        return 1
    worker_cfg = json.loads(worker_val.decode('utf-8'))
    
    _, res, msg = create_one_node(
        name=args.node,
        node=node_cfg,
        workers={worker_name: worker_cfg},
        etcd_host=args.node_etcd_host,
        etcd_port=args.node_etcd_port,
        etcd_user=args.etcd_user,
        etcd_password=args.etcd_password,
        etcd_ca_cert=args.etcd_ca_cert,
    )

    # Print per-node result
    if node_cfg.get("type", "satellite") == "satellite":
        prefix = "🛰️"
    elif node_cfg.get("type", "satellite") == "user":
        prefix = "👤"
    elif node_cfg.get("type", "satellite") == "gateway":
        prefix = "📡"
    else:
        prefix = "🛰️"
    
    log.info(f"{prefix} {args.node}: {msg}")

if __name__ == "__main__":
    raise SystemExit(main())
