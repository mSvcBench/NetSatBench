#!/usr/bin/env python3
import argparse
import concurrent.futures
import etcd3
import subprocess
import json
import os
import sys
import shlex
from typing import Dict, Any, Tuple, Optional

# ==========================================
# 🚩 CONFIGURATION
# ==========================================
ETCD_HOST = os.getenv('ETCD_HOST', '127.0.0.1')
ETCD_PORT = int(os.getenv('ETCD_PORT', 2379))


# ==========================================
# HELPERS
# ==========================================
def connect_etcd(host: str, port: int):
    try:
        print(f"📁 Connecting to Etcd at {host}:{port}...")
        return etcd3.client(host=host, port=port)
    except Exception as e:
        print(f"❌ Failed to initialize Etcd client: {e}")
        sys.exit(1)


def get_prefix_data(etcd, prefix: str) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for value, metadata in etcd.get_prefix(prefix):
        key = metadata.key.decode('utf-8').split('/')[-1]
        try:
            data[key] = json.loads(value.decode('utf-8'))
        except json.JSONDecodeError:
            print(f"⚠️ Warning: Could not parse JSON for key {key} under {prefix}")
    return data


class SshError(RuntimeError):
    pass

class RemoteCommandError(RuntimeError):
    pass

def run_ssh(
    *,
    ssh_username: str,
    sat_host: str,
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
        f"{ssh_username}@{sat_host}",
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
        raise SshError(f"SSH timeout connecting to {ssh_username}@{sat_host}")

    stderr = (cp.stderr or "").strip()

    # SSH transport failures are typically exit code 255
    if cp.returncode == 255:
        msg = stderr.splitlines()[0] if stderr else "SSH transport error"
        raise SshError(msg)

    if check and cp.returncode != 0:
        msg = stderr.splitlines()[0] if stderr else "Remote command failed"
        raise RemoteCommandError(msg)

    return cp

def recreate_and_run_container(
    *,
    sat_name: str,
    sat_host: str,
    ssh_username: str,
    ssh_key_path: str,
    sat_host_bridge_name: str,
    container_image: str,
    etcd_host: str,
    etcd_port: int,
) -> None:

    try:
        # --- Check if container exists ---
        ps = run_ssh(
            ssh_username=ssh_username,
            sat_host=sat_host,
            ssh_key_path=ssh_key_path,
            remote_args=["docker", "ps", "-a", "--format", "{{.Names}}"],
            check=True,  # if docker command fails, raise RemoteCommandError
        )

        names = ps.stdout.splitlines() if ps.stdout else []
        exists = sat_name in names

        # --- Remove existing container (ignore errors, but still raise SSH transport errors) ---
        if exists:
            run_ssh(
                ssh_username=ssh_username,
                sat_host=sat_host,
                ssh_key_path=ssh_key_path,
                remote_args=["docker", "rm", "-f", sat_name],
                check=False,   # ignore non-zero from docker rm
                quiet=True,
            )
        # --- Run new container ---
        run_cmd = [
            "docker", "run", "-d",
            "--name", sat_name,
            "--hostname", sat_name,
            "--net", sat_host_bridge_name,
            "--privileged",
            "--pull=always",
            "-e", f"SAT_NAME={sat_name}",
            "-e", f"ETCD_ENDPOINT={etcd_host}:{etcd_port}",
            container_image,
        ]
        run_ssh(
            ssh_username=ssh_username,
            sat_host=sat_host,
            ssh_key_path=ssh_key_path,
            remote_args=run_cmd,
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
    hosts: Dict[str, Any],
    etcd_host: str,
    etcd_port: int
) -> Tuple[str, bool, str]:
    """
    Returns: (name, success, message)
    """
    node_host = node.get('host')
    if not node_host:
        return name, False, "❌ Missing 'host' field in node config"

    if node_host not in hosts:
        return name, False, f"❌ Unknown host '{node_host}' (node assigned to non-existing /config/hosts entry)"

    host_info = hosts[node_host]
    ssh_user = host_info.get('ssh_user', 'ubuntu')
    ssh_key = host_info.get('ssh_key', '~/.ssh/id_rsa')
    sat_bridge = host_info.get('sat-vnet', 'sat-vnet')
    image = node.get('image', 'msvcbench/sat-container:latest')

    try:
        cmd = recreate_and_run_container(
            sat_name=name,
            sat_host=node_host,
            ssh_username=ssh_user,
            ssh_key_path=ssh_key,
            sat_host_bridge_name=sat_bridge,
            container_image=image,
            etcd_host=etcd_host,
            etcd_port=etcd_port,
        )
        msg = f" Created on host={node_host}"
        return name, True, msg
    except Exception as e:
        return name, False, f"❌ Deployment failed: {e}"
    

# ==========================================
# MAIN
# ==========================================
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deploy constellation nodes (satellites/users/grounds) by creating containers in parallel."
    )
    parser.add_argument(
        "-t", "--threads",
        type=int,
        default=max(1, (os.cpu_count() or 4)),
        help="Number of worker threads for parallel container creation (default: CPU count).",
    )
    parser.add_argument(
        "--only",
        choices=["satellites", "users", "grounds", "all"],
        default="all",
        help="Select which node types to deploy (default: all).",
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

    if args.threads < 1:
        print("❌ --threads must be >= 1")
        return 2

    etcd = connect_etcd(ETCD_HOST, ETCD_PORT)

    # 1) LOAD CONFIGURATION
    satellites = get_prefix_data(etcd, '/config/satellites/')
    users = get_prefix_data(etcd, '/config/users/')
    grounds = get_prefix_data(etcd, '/config/grounds/')
    hosts = get_prefix_data(etcd, '/config/hosts/')

    print(f"🔎 Found {len(satellites)} satellites, {len(users)} users, and {len(grounds)} grounds in Etcd.")

    if args.only == "satellites":
        all_nodes = satellites
    elif args.only == "users":
        all_nodes = users
    elif args.only == "grounds":
        all_nodes = grounds
    else:
        all_nodes = {**satellites, **users, **grounds}
    
    if not all_nodes:
        print("⚠️ Warning: No nodes found. Run 'init.py' to populate Etcd first.")
        return 1

    if not hosts:
        print("❌ Error: No hosts found in /config/hosts/. Cannot deploy.")
        return 1

    # 2) CREATE CONTAINERS IN PARALLEL
    print(f"🚀 Deploying {args.only} nodes using {args.threads} threads...")

    ok = 0
    fail = 0

    # If you want to avoid overloading a single remote host, a future enhancement is
    # to add per-host semaphores. For now, this is global parallelism.
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.threads) as executor:
        futures = {}
        for name, node in all_nodes.items():
            future = executor.submit(
                create_one_node,
                name,
                node,
                hosts,
                ETCD_HOST,
                ETCD_PORT
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
            if node_name in satellites:
                prefix = "🛰️"
            elif node_name in users:
                prefix = "👤"
            elif node_name in grounds:
                prefix = "📡"
            
            print(f"{prefix} {node_name}: {msg}")

            if success:
                ok += 1
            else:
                fail += 1

    print("\n==============================")
    print(f"✅ Success: {ok}")
    print(f"❌ Failed : {fail}")
    print("==============================")

    if fail == 0:
        print("\n✅ Constellation Build Complete.")
        return 0
    else:
        print("\n⚠️ Constellation Build Completed with failures.")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
