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
        msg = f" Created on worker={worker}"
        return name, True, msg
    except Exception as e:
        return name, False, f"❌ Deployment failed: {e}"
    

# ==========================================
# MAIN
# ==========================================
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deploy nodes (satellites/users/grounds) by creating containers in parallel."
    )
    parser.add_argument(
        "-t", "--threads",
        type=int,
        default=4,
        help="Number of worker threads for parallel container creation (default: 4).",
    )
    parser.add_argument(
        "--type",
        default="any",
        help="Select which node types to deploy in a comma-separated list (default: any).",
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
    parser.add_argument("--fix", action="store_true", help="Fix mode: check existing nodes and redeploy those with no eth0_ip configured.")
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (default: INFO)",
    )
    args = parser.parse_args()

    log.setLevel(args.log_level.upper())

    if args.threads < 1:
        log.error("❌ --threads must be >= 1")
        return 2

    etcd_client = connect_etcd(args.etcd_host, args.etcd_port, args.etcd_user, args.etcd_password, args.etcd_ca_cert)

    try:
        no_nodes = True
        existing_nodes = etcd_client.get_prefix("/config/nodes/")
        for node in existing_nodes:
            no_nodes=False
            node_config = json.loads(node[0].decode('utf-8')) if node[0] else None
            if "eth0_ip" in node_config and not args.fix:
                log.warning("⚠️  Nodes already found in Etcd under /config/nodes/ with eth0_ip configured. This may indicate nsb-deploy has been already run.")
                cont = input("Do you want to continue with nsb-deploy? (y/n): ")
                if cont.lower() != 'y':
                    log.info("Exiting as per user request.")
                    sys.exit(0)
    except Exception as e:
        log.error(f"❌ Error checking existing nodes in Etcd: {e}")
        sys.exit(1)

    if no_nodes:
        log.error("❌ No nodes found in Etcd under /config/nodes/. This may indicate nsb-init has not been run.")
        sys.exit(0)
            
    # 1) LOAD CONFIGURATION
    workers = get_prefix_data(etcd_client, '/config/workers/')
    all_nodes = get_prefix_data(etcd_client, '/config/nodes/')
    all_nodes_filtered = {}
    node_types = args.type.split(",")
    if "any" in node_types:
        all_nodes_filtered = all_nodes
    else:
        for node_type in node_types:
            all_nodes_filtered = {name:node for name,node in all_nodes.items() if node.get("type","undefined") == node_type}

    if args.fix:
         log.info("🔧 Fix mode enabled: will check existing nodes and redeploy those with no eth0_ip configured.")
         all_nodes_filtered = {name:node for name,node in all_nodes_filtered.items() if node.get("eth0_ip", None) is None}
    
    log.info(f"🔎 Found {len(all_nodes_filtered)} nodes, to deploy.")
    
    if not all_nodes_filtered:
        log.warning("⚠️ Warning: No nodes found to deploy")
        return 1

    if not workers:
        log.error("❌ Error: No workers found in /config/workers/. Cannot deploy.")
        return 1

    # 2) CREATE CONTAINERS IN PARALLEL
    log.info(f"🚀 Deploying {args.type} nodes using {args.threads} threads...")
    log.info("-" * 50)

    ok = 0
    fail = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.threads) as executor:
        futures = {}
        for name, node in all_nodes_filtered.items():
            future = executor.submit(
                create_one_node,
                name,
                node,
                workers,
                etcd_host=args.node_etcd_host,
                etcd_port=args.node_etcd_port,
                etcd_user=args.etcd_user,
                etcd_password=args.etcd_password,
                etcd_ca_cert=args.etcd_ca_cert,
            )
            futures[future] = name

        for fut in concurrent.futures.as_completed(futures):
            name = futures[fut]
            try:
                node_name, success, msg = fut.result()
            except Exception as e:
                success = False
                msg = f"❌ Unhandled exception: {e}"
                node_name = name

            # Print per-node result
            if all_nodes_filtered[node_name].get("type") == "satellite":
                prefix = "🛰️"
            elif all_nodes_filtered[node_name].get("type") == "user":
                prefix = "👤"
            elif all_nodes_filtered[node_name].get("type") == "gateway":
                prefix = "📡"
            else:
                prefix = "🛰️"
            
            log.info(f"{prefix} {node_name}: {msg}")

            if success:
                ok += 1
            else:
                fail += 1
    log.info("-" * 50)
    if fail == 0:
        log.info("✅ Satellite system deployment completed...waiting for nodes to come online.")
    else:
        log.warning(f"⚠️ Satellite system deployment completed with {fail} failures.")
        return 3

    # wait that all deployed node have put their eth0_ip in etcd
    all_ready = False
    for _ in range(60):  # wait up to 60 seconds for all nodes to report in
        all_ready = True
        for name, node in all_nodes_filtered.items():
            val, _ = etcd_client.get(f"/config/nodes/{name}")   
            val = json.loads(val.decode('utf-8')) if val else None
            if 'eth0_ip' not in val:
                all_ready = False
                break
        if all_ready:
            break
        time.sleep(1)
    time.sleep(5)  # extra wait to ensure all services inside the containers are up
    if not all_ready:
        log.warning("⚠️ Some nodes did not report their eth0_ip in Etcd within the expected time. Could be an Etcd connection problem")
    else:
        log.info("👍 Satellite system deployment completed and all nodes running.")
        log.info("▶️ Proceed with nsb.py run to parse epoch files and start the emulation.")

if __name__ == "__main__":
    raise SystemExit(main())
