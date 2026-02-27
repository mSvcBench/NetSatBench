#!/usr/bin/env python3
import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import List

import etcd3

logging.basicConfig(level="INFO", format="[%(levelname)s] %(message)s")
log = logging.getLogger("nsb-exectype")


def get_nodes_by_type(etcd_client, wanted_type: str) -> List[str]:
    nodes: List[str] = []
    wanted = wanted_type.lower()

    for val, meta in etcd_client.get_prefix("/config/nodes/"):
        if not val:
            continue
        try:
            node_cfg = json.loads(val.decode("utf-8"))
        except Exception:
            continue

        if (node_cfg.get("type") or "").lower() != wanted:
            continue

        key = meta.key.decode("utf-8")
        node_name = key.rsplit("/", 1)[-1]
        if node_name:
            nodes.append(node_name)

    return sorted(nodes)


def build_nsb_exec_cmd(args, node: str) -> List[str]:
    nsb_exec_path = str(Path(__file__).with_name("nsb-exec.py"))
    cmd = [sys.executable, nsb_exec_path]

    cmd.extend([
        "--etcd-host",
        args.etcd_host,
        "--etcd-port",
        str(args.etcd_port),
    ])
    if args.etcd_user:
        cmd.extend(["--etcd-user", args.etcd_user])
    if args.etcd_password:
        cmd.extend(["--etcd-password", args.etcd_password])
    if args.etcd_ca_cert:
        cmd.extend(["--etcd-ca-cert", args.etcd_ca_cert])

    if args.detached:
        cmd.append("-d")

    cmd.extend(["--log-level", args.log_level, node])
    cmd.extend(args.command)
    return cmd


def main() -> int:
    if "-it" in sys.argv or "--interactive" in sys.argv:
        log.error("❌ -it/--interactive is not supported by nsb-exectype")
        return 2

    p = argparse.ArgumentParser(
        prog="nsb-exectype",
        description="Execute a command on all nodes of a given type using nsb-exec.",
    )

    p.add_argument(
        "-d",
        "--detached",
        action="store_true",
        help="Run in detached mode (docker exec -d).",
    )

    p.add_argument("--etcd-host", default=os.getenv("ETCD_HOST", "127.0.0.1"), help="Etcd host (default: 127.0.0.1)")
    p.add_argument("--etcd-port", type=int, default=int(os.getenv("ETCD_PORT", 2379)), help="Etcd port (default: 2379)")
    p.add_argument("--etcd-user", default=os.getenv("ETCD_USER", None), help="Etcd user (default: None)")
    p.add_argument("--etcd-password", default=os.getenv("ETCD_PASSWORD", None), help="Etcd password (default: None)")
    p.add_argument("--etcd-ca-cert", default=os.getenv("ETCD_CA_CERT", None), help="Etcd CA certificate path (default: None)")
    p.add_argument("--log-level", default="INFO")

    p.add_argument("node_type", help="Target node type (e.g., satellite, gateway, user)")
    p.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Command to run inside each matching node container.",
    )

    args = p.parse_args()
    log.setLevel(args.log_level.upper())

    if not args.command:
        log.error("❌ Missing command. Example: nsb.py exectype satellite ip a")
        return 2

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
        return 1

    nodes = get_nodes_by_type(etcd_client, args.node_type)
    if not nodes:
        log.error(f"❌ No nodes found with type '{args.node_type}'")
        return 1

    log.info(f"🔎 Found {len(nodes)} nodes of type '{args.node_type}': {', '.join(nodes)}")

    for node in nodes:
        log.info(f"▶️ Executing on node '{node}'")
        cmd = build_nsb_exec_cmd(args, node)
        rc = subprocess.run(cmd).returncode
        if rc != 0:
            log.error(f"❌ Command failed on node '{node}' (exit={rc})")
            return rc

    return 0


if __name__ == "__main__":
    sys.exit(main())
