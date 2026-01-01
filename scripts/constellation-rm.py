#!/usr/bin/env python3
import argparse
import os
import shlex
import concurrent
import etcd3
import subprocess
import json
import sys

# ==========================================
# 🚩 CONFIGURATION
# ==========================================
ETCD_HOST = os.getenv('ETCD_HOST', '127.0.0.1')
ETCD_PORT = int(os.getenv('ETCD_PORT', 2379))


# ==========================================
# HELPERS
# ==========================================
def connect_etcd(host: str, port: int):
    try:
        print(f"📁 Connecting to Etcd at {host}:{port}...")
        return etcd3.client(host=host, port=port)
    except Exception as e:
        print(f"❌ Failed to initialize Etcd client: {e}")
        sys.exit(1)

def get_prefix_data(cli,prefix):
    """Helper to fetch and parse JSON data from Etcd prefixes."""
    data = {}
    for value, metadata in cli.get_prefix(prefix):
        key = metadata.key.decode('utf-8').split('/')[-1]
        try:
            data[key] = json.loads(value.decode('utf-8'))
        except json.JSONDecodeError:
            pass
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

def node_removal(ssh_user: str, ssh_ip: str, ssh_key: str, name: str, node_host: str)-> tuple[str, bool, str]:
    try:
            # ----------------------------------------
            # Check if container exists
            # ----------------------------------------
            ps = run_ssh(
                    ssh_username=ssh_user,
                    sat_host=ssh_ip,
                    ssh_key_path=ssh_key,
                    remote_args=["docker", "ps", "-a", "--format", "{{.Names}}","|","grep","-Fxq", name],
                    check=True
                )
    except SshError as e:
        msg = f"    ❌ SSH failure: {e}"
        return name, False, msg

    except RemoteCommandError:
        msg = f"   ⚠️  Container '{name}' does not exist. Skipping."
        return name, True, msg
    # ----------------------------------------
    # Stop and Remove
    # ----------------------------------------
    try:
        del_proc = run_ssh(
                ssh_username=ssh_user,
                sat_host=ssh_ip,
                ssh_key_path=ssh_key,
                remote_args=["docker", "rm", "-f", name],
                check=True
            )
    except SshError as e:
        msg = f"    ❌ SSH failure: {e}"
        return name, False, msg
    except RemoteCommandError as e:
        msg = f"    ❌ Remote docker command failed: {e}"
        return name, False, msg
    
    msg = f" Removed on {node_host}"
    return name, True, msg

def main():
    parser = argparse.ArgumentParser(
        description="Remove constellation nodes (satellites/users/grounds) by removing containers in parallel."
    )
    parser.add_argument(
        "-t", "--threads",
        type=int,
        default=max(1, (os.cpu_count() or 4)),
        help="Number of worker threads for parallel container removal (default: CPU count).",
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


    # ==========================================
    # LOAD CONFIGURATION
    # ==========================================
    cli = connect_etcd(args.etcd_host, args.etcd_port)

    # Fetch all relevant configuration
    satellites = get_prefix_data(cli, '/config/satellites/')
    users = get_prefix_data(cli, '/config/users/')
    grounds = get_prefix_data(cli, '/config/grounds/')
    hosts = get_prefix_data(cli, '/config/hosts/')

    # Merge satellites and users into one list of nodes to clean up
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
        print("⚠️  No nodes found in Etcd. Nothing to do.")

    # ==========================================
    # DELETE CONTAINERS (Threaded)
    # ==========================================
    print(f"\n🧹 Starting Cleanup Process for {args.only} nodes using {args.threads} threads...")
    print("-" * 50)
    
    ok = 0
    fail = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.threads) as executor:
        futures = {}
        for name, node in all_nodes.items():
            node_host = node.get('host')
            # Validation: Does the host exist in config?
            if node_host not in hosts:
                print(f"⚠️  Skipping {name}: Host '{node_host}' not found in /config/hosts")
                continue
            future = executor.submit(
                node_removal,
                ssh_user=hosts.get(node_host, {}).get('ssh_user', 'ubuntu'),
                ssh_ip=hosts.get(node_host, {}).get('ip', node_host),
                ssh_key=hosts.get(node_host, {}).get('ssh_key', '~/.ssh/id_rsa'),
                name=name, node_host=node_host
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
        print("\n✅ Constellation Cleaning Complete.")
    else:
        print("\n⚠️ Constellation Cleaning Completed with failures.")
        
    # ==========================================
    # CLEAN ETCD ENTRIES
    # ==========================================
    print("\n🧼 Cleaning up Etcd entries...")
    prefixes = ['/']
    for prefix in prefixes:
        print(f"   ➞ Deleting keys with prefix {prefix} ...")
        cli.delete_prefix(prefix)

    print("-" * 50)
    print("✅ Global Cleanup Complete.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())