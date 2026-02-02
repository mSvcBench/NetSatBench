#!/usr/bin/env python3
import argparse
import json
import logging
import os
import subprocess
import sys
from typing import Optional, Tuple

import etcd3

logging.basicConfig(level="INFO", format="[%(levelname)s] %(message)s")
log = logging.getLogger("constellation-cp")


# =========================
# Etcd helpers
# =========================
def get_json(etcd_client, key: str) -> Optional[dict]:
    val, _ = etcd_client.get(key)
    if not val:
        return None
    try:
        return json.loads(val.decode("utf-8"))
    except Exception:
        return None


def split_node_spec(s: str) -> Optional[Tuple[str, str]]:
    """Parse NODE:/path"""
    if ":" not in s:
        return None
    node, path = s.split(":", 1)
    if not node or not path:
        return None
    return node, path


def node_prefix(node_cfg: dict) -> str:
    t = (node_cfg.get("type") or "").lower()
    return {"satellite": "🛰️", "user": "👤", "gateway": "📡"}.get(t, "🛰️")


# =========================
# Main
# =========================
def main() -> int:
    p = argparse.ArgumentParser(
        prog="constellation-cp",
        description="Copy files between local host and a constellation node (docker cp over SSH, local semantics).",
    )

    # Etcd
    p.add_argument("--etcd-host", default=os.getenv("ETCD_HOST", "127.0.0.1"))
    p.add_argument("--etcd-port", type=int, default=int(os.getenv("ETCD_PORT", "2379")))
    p.add_argument("--etcd-user", default=os.getenv("ETCD_USER"))
    p.add_argument("--etcd-password", default=os.getenv("ETCD_PASSWORD"))
    p.add_argument("--etcd-ca-cert", default=os.getenv("ETCD_CA_CERT"))

    # docker cp options
    p.add_argument("-L", "--follow-link", action="store_true")
    p.add_argument("-a", "--archive", action="store_true")

    p.add_argument("--log-level", default="INFO")
    p.add_argument("src")
    p.add_argument("dest")

    args = p.parse_args()
    log.setLevel(args.log_level.upper())

    src_spec = split_node_spec(args.src)
    dest_spec = split_node_spec(args.dest)

    if src_spec and dest_spec:
        log.error("❌ NODE:PATH → NODE:PATH is not supported")
        return 2
    if not src_spec and not dest_spec:
        log.error("❌ Either SRC or DEST must be NODE:PATH")
        return 2

    # Direction
    if src_spec:
        node, node_path = src_spec
        direction = "FROM_NODE"
    else:
        node, node_path = dest_spec
        direction = "TO_NODE"

    # =========================
    # Etcd lookup
    # =========================
    try:
        etcd = (
            etcd3.client(
                host=args.etcd_host,
                port=args.etcd_port,
                user=args.etcd_user,
                password=args.etcd_password,
                ca_cert=args.etcd_ca_cert,
            )
            if args.etcd_user and args.etcd_password and args.etcd_ca_cert
            else etcd3.client(host=args.etcd_host, port=args.etcd_port)
        )
    except Exception as e:
        log.error(f"❌ Etcd init failed: {e}")
        return 1

    node_cfg = get_json(etcd, f"/config/nodes/{node}")
    if not node_cfg:
        log.error(f"❌ Node '{node}' not found")
        return 1

    worker_cfg = get_json(etcd, f"/config/workers/{node_cfg.get('worker')}")
    if not worker_cfg:
        log.error("❌ Worker not found")
        return 1

    ssh_user = worker_cfg.get("ssh-user", "ubuntu")
    worker_ip = worker_cfg.get("ip", node_cfg.get("worker"))
    ssh_key = os.path.expanduser(worker_cfg.get("ssh-key", "~/.ssh/id_rsa"))

    log.info(f"{node_prefix(node_cfg)} node='{node}' on {ssh_user}@{worker_ip}")

    # =========================
    # Build docker cp flags
    # =========================
    cp_flags = []
    if args.follow_link:
        cp_flags.append("-L")
    if args.archive:
        cp_flags.append("-a")

    ssh_base = [
        "ssh",
        "-i",
        ssh_key,
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        f"{ssh_user}@{worker_ip}",
    ]

    try:
        if direction == "FROM_NODE":
            # docker cp NODE:/path - | tar x (locally)
            docker_cmd = ["docker", "cp", *cp_flags, f"{node}:{node_path}", "-"]
            ssh_cmd = ssh_base + docker_cmd

            log.info("📥 Copying from node to local host")
            with subprocess.Popen(ssh_cmd, stdout=subprocess.PIPE) as p_ssh:
                res = subprocess.run(
                    ["tar", "xf", "-", "-C", args.dest],
                    stdin=p_ssh.stdout,
                )
                return res.returncode

        else:
            # tar c (locally) | ssh docker cp - NODE:/path
            docker_cmd = ["docker", "cp", *cp_flags, "-", f"{node}:{node_path}"]
            ssh_cmd = ssh_base + docker_cmd

            log.info("📤 Copying from local host to node")
            tar_cmd = ["tar", "cf", "-", "-C", os.path.dirname(args.src) or ".", os.path.basename(args.src)]

            p_tar = subprocess.Popen(tar_cmd, stdout=subprocess.PIPE)
            p_ssh = subprocess.run(ssh_cmd, stdin=p_tar.stdout)
            return p_ssh.returncode

    except KeyboardInterrupt:
        return 130
    except Exception as e:
        log.error(f"❌ Copy failed: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
