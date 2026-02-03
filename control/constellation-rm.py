#!/usr/bin/env python3
import argparse
import os
import logging
import concurrent
import etcd3
import subprocess
import json
import sys

logging.basicConfig(level="INFO", format="[%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ==========================================
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
            client = etcd3.client(host=etcd_host, port=etcd_port, user=etcd_user, password=etcd_password, ca_cert=etcd_ca_cert)
            client.status()  # Test connection, if fail will raise
            return client
    except Exception as e:
        log.error(f"❌ Failed to initialize Etcd client: {e}")
        sys.exit(1)

def get_prefix_data(etcd_client,prefix):
    """Helper to fetch and parse JSON data from Etcd prefixes."""
    data = {}
    for value, metadata in etcd_client.get_prefix(prefix):
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
        raise RemoteCommandError(msg)

    return cp

def node_removal(ssh_user: str, ssh_host: str, ssh_key: str, name: str, worker: str)-> tuple[str, bool, str]:
    try:
            # ----------------------------------------
            # Check if container exists
            # ----------------------------------------
        ps = run_ssh(
                ssh_username=ssh_user,
                ssh_host=ssh_host,
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
                ssh_host=ssh_host,
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
    
    msg = f" Removed on {worker}"
    return name, True, msg

def main():
    parser = argparse.ArgumentParser(
        description="Remove constellation nodes (satellites/users/grounds) by removing containers in parallel."
    )
    parser.add_argument(
        "-t", "--threads",
        type=int,
        default=4,
        help="Number of worker threads for parallel container removal (default: 4).",
    )
    parser.add_argument(
        "--type",
        default="any",
        help="Select which node types to deploy in a comma-separated list (default: any).",
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


    # ==========================================
    # LOAD CONFIGURATION
    # ==========================================
    etcd_client = connect_etcd(args.etcd_host, args.etcd_port, args.etcd_user, args.etcd_password, args.etcd_ca_cert)

    # Fetch all relevant configuration
    workers = get_prefix_data(etcd_client, '/config/workers/')
    all_nodes = get_prefix_data(etcd_client, '/config/nodes/')
    all_nodes_filtered = {}
    node_types = args.type.split(",")
    if "any" in node_types:
        all_nodes_filtered = all_nodes
    else:
        for node_type in node_types:
            all_nodes_filtered = {name:node for name,node in all_nodes.items() if node.get("type","undefined") == node_type}

    log.info(f"🔎 Found {len(all_nodes_filtered)} nodes, to remove.")

    if not all_nodes_filtered:
        log.warning("⚠️  No nodes found in Etcd. Nothing to do.")

    # ==========================================
    # DELETE CONTAINERS (Threaded)
    # ==========================================
    log.info(f"🧹 Starting Cleanup Process for {args.type} nodes using {args.threads} threads...")
    log.info("-" * 50)
    
    ok = 0
    fail = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.threads) as executor:
        futures = {}
        for name, node in all_nodes_filtered.items():
            worker = node.get('worker')
            # Validation: Does the host exist in config?
            if worker not in workers:
                log.warning(f"⚠️  Skipping {name}: Worker '{worker}' not found in /config/workers")
                continue
            future = executor.submit(
                node_removal,
                ssh_user=workers.get(worker, {}).get('ssh-user', 'ubuntu'),
                ssh_host=workers.get(worker, {}).get('ip', worker),
                ssh_key=workers.get(worker, {}).get('ssh-key', '~/.ssh/id_rsa'),
                name=name, worker=worker
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
            if all_nodes_filtered[node_name].get("type","undefined") == "satellite":
                prefix = "🛰️"
            elif all_nodes_filtered[node_name].get("type","undefined") == "user":
                prefix = "👤"
            elif all_nodes_filtered[node_name].get("type","undefined") == "gateway":
                prefix = "📡"
            else:
                prefix = "🛰️"
            
            log.info(f"{prefix} {node_name}: {msg}")

            if success:
                ok += 1
            else:
                fail += 1

    log.info("==============================")
    log.info(f"✅ Success: {ok}")
    log.info(f"❌ Failed : {fail}")
    log.info("==============================")

    if fail != 0:
        log.warning("⚠️ Constellation Cleaning Completed with failures.")
        
    # ==========================================
    # CLEAN ETCD ENTRIES
    # ==========================================
    log.info("🧼 Cleaning up Etcd entries...")
    prefixes = ["/config/nodes/", "/config/epoch-config", "/config/links/", "/config/run","/config/etchosts/"]
    for prefix in prefixes:
        log.info(f"   ➞ Deleting keys with prefix {prefix} ...")
        etcd_client.delete_prefix(prefix)
    #cleanup workers' usage stats
    log.info(f"   ➞ Resetting workers' usage stats...")
    for name, worker_cfg in workers.items():
        # Reset usage
        worker_cfg['cpu-used'] = 0.0
        worker_cfg['mem-used'] = 0.0  
        key = f"/config/workers/{name}"
        etcd_client.put(key, json.dumps(worker_cfg))
        
    log.info("-" * 50)
    log.info("👍 Global Cleanup Complete.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())