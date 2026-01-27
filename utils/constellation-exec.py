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

def get_node_cfg(etcd_client, node_name):
    val, _ = etcd_client.get(f"/config/nodes/{node_name}")
    if val:
        try:
            return json.loads(val.decode())
        except Exception:
            pass
    return None


def main():
    global etcd_client

    parser = argparse.ArgumentParser(
        description="Execute docker exec on the remote worker hosting a constellation node."
    )
    parser.add_argument(
        "-it", "--interactive",
        action="store_true",
        help="Run in interactive mode (allocate TTY and attach)."
    )
    parser.add_argument(
        "-d", "--detached",
        action="store_true",
        help="Run in detached mode (docker exec -d)."
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
    parser.add_argument(
        "node",
        help="Target constellation node/container name"
    )
    # IMPORTANT: remainder so you can do: exec.py sat1 bash -lc 'echo hi'
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Command to run inside the container (everything after NODE). Example: bash -lc 'ip a'"
    )

    args = parser.parse_args()
    log.setLevel(args.log_level.upper())

    if args.interactive and args.detached:
        log.error("❌ --interactive and --detached are mutually exclusive.")
        sys.exit(2)

    if not args.command:
        log.error("❌ Missing command. Example: ./exec.py sat1 bash -lc 'ip route'")
        sys.exit(2)

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
    # RESOLVE WORKER
    # ==========================================
    node_cfg = get_node_cfg(etcd_client, args.node)
    if not node_cfg:
        log.error(f"❌ Node '{args.node}' not found in Etcd.")
        sys.exit(1)
    worker_name = node_cfg.get("worker", None)
    if not worker_name:
        log.error(f"❌ Worker of node '{args.node}' not found in Etcd.")
        sys.exit(1)

    val, _ = etcd_client.get(f"/config/workers/{worker_name}")
    if not val:
        log.error(f"❌ Worker '{worker_name}' not found in Etcd.")
        sys.exit(1)

    try:
        worker = json.loads(val.decode())
    except Exception as e:
        log.error(f"❌ Failed to parse worker '{worker_name}' configuration: {e}")
        sys.exit(1)

    ssh_user = worker.get("ssh_user", "ubuntu")
    worker_ip = worker.get("ip", worker_name)
    ssh_key = worker.get("ssh_key", "~/.ssh/id_rsa")
    ssh_key = os.path.expanduser(ssh_key)
    
    if node_cfg.get("type") == "satellite":
        prefix = "🛰️"
    elif node_cfg.get("type") == "user":
        prefix = "👤"
    elif node_cfg.get("type") == "gateway":
        prefix = "📡"
    else:
        prefix = "🛰️"
    log.info(f"{prefix} Target node='{args.node}' worker='{worker_name}' ({ssh_user}@{worker_ip})")

    # ==========================================
    # BUILD COMMAND SAFELY (no shell=True)
    # ==========================================
    docker_cmd = ["docker", "exec"]
    if args.interactive:
        docker_cmd += ["-it"]
    if args.detached:
        docker_cmd += ["-d"]
    docker_cmd += [args.node] + args.command

    # ssh options:
    # -i: key
    # -tt only when interactive, to force pseudo-tty allocation even if local stdin isn't a tty
    ssh_cmd = ["ssh", "-i", ssh_key]
    if args.interactive:
        ssh_cmd += ["-tt"]
    ssh_cmd += [f"{ssh_user}@{worker_ip}"] + docker_cmd

    # ==========================================
    # EXECUTE
    # ==========================================
    try:
        # In interactive mode: inherit stdin/stdout/stderr so it feels like "docker exec -it"
        res = subprocess.run(ssh_cmd, check=False)
        sys.exit(res.returncode)
    except FileNotFoundError as e:
        log.error(f"❌ Missing executable: {e}")
        sys.exit(127)
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        log.error(f"❌ Failed to execute remote command: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
