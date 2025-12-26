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


def build_create_cmd(
    name: str,
    node_host: str,
    ssh_user: str,
    ssh_key: str,
    sat_bridge: str,
    image: str,
    etcd_host: str,
    etcd_port: int,
) -> list:
    # Usage: ./create-sat.sh <SAT_NAME> [SAT_HOST] [SSH_USERNAME] [SSH_KEY_PATH]
    #                         [ETCD_HOST] [ETCD_PORT] [SAT_HOST_BRIDGE_NAME] [CONTAINER_IMAGE]
    return [
        'scripts/create-sat.sh',
        name,
        node_host,
        ssh_user,
        ssh_key,
        etcd_host,
        str(etcd_port),
        sat_bridge,
        image,
    ]


def create_one_node(
    name: str,
    node: Dict[str, Any],
    hosts: Dict[str, Any],
    etcd_host: str,
    etcd_port: int,
    dry_run: bool = False,
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

    cmd = build_create_cmd(
        name=name,
        node_host=node_host,
        ssh_user=ssh_user,
        ssh_key=ssh_key,
        sat_bridge=sat_bridge,
        image=image,
        etcd_host=etcd_host,
        etcd_port=etcd_port,
    )

    pretty = " ".join(shlex.quote(x) for x in cmd)

    if dry_run:
        return name, True, f"🧪 DRY-RUN: {pretty}"

    try:
        res = subprocess.run(
            cmd,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # Keep messages compact; include stderr only if non-empty.
        msg = f"✅ Created on host={node_host}"
        # if res.stderr.strip():
        #     msg += f" (stderr: {res.stderr.strip()})"
        return name, True, msg
    except subprocess.CalledProcessError as e:
        out = (e.stdout or "").strip()
        err = (e.stderr or "").strip()
        msg = f"❌ Failed (exit {e.returncode})"
        if out:
            msg += f"\nSTDOUT:\n{out}"
        if err:
            msg += f"\nSTDERR:\n{err}"
        return name, False, msg


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
        "--dry-run",
        action="store_true",
        help="Do not execute create-sat.sh, only print the commands that would be run.",
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
    print(f"🚀 Deploying {len(all_nodes)} nodes using {args.threads} thread(s)...")

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
                ETCD_PORT,
                args.dry_run,
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
            prefix = "🛰️"
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
