#!/usr/bin/env python3
import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

import etcd3

logging.basicConfig(level="INFO", format="[%(levelname)s] %(message)s")
log = logging.getLogger("nsb-cptype")


def split_type_spec(s: str) -> Optional[Tuple[str, str]]:
    """Parse TYPE:/path"""
    if ":" not in s:
        return None
    node_type, path = s.split(":", 1)
    if not node_type or not path:
        return None
    return node_type, path


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


def build_nsb_cp_cmd(args, src: str, dest: str) -> List[str]:
    nsb_cp_path = str(Path(__file__).with_name("nsb-cp.py"))
    cmd = [sys.executable, nsb_cp_path]

    # Forward etcd args explicitly for deterministic behavior
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

    if args.follow_link:
        cmd.append("-L")
    if args.archive:
        cmd.append("-a")

    cmd.extend(["--log-level", args.log_level, src, dest])
    return cmd


def safe_replace(src: Path, dst: Path) -> None:
    if dst.exists() or dst.is_symlink():
        if dst.is_dir() and not dst.is_symlink():
            shutil.rmtree(dst)
        else:
            dst.unlink()
    shutil.move(str(src), str(dst))


def main() -> int:
    p = argparse.ArgumentParser(
        prog="nsb-cptype",
        description="Copy files between local host and all nodes of a given type using nsb-cp.",
    )

    # Etcd
    p.add_argument("--etcd-host", default=os.getenv("ETCD_HOST", "127.0.0.1"), help="Etcd host (default: 127.0.0.1)")
    p.add_argument("--etcd-port", type=int, default=int(os.getenv("ETCD_PORT", "2379")), help="Etcd port (default: 2379)")
    p.add_argument("--etcd-user", default=os.getenv("ETCD_USER"), help="Etcd user (default: None)")
    p.add_argument("--etcd-password", default=os.getenv("ETCD_PASSWORD"), help="Etcd password (default: None)")
    p.add_argument("--etcd-ca-cert", default=os.getenv("ETCD_CA_CERT"), help="Etcd CA certificate path (default: None)")

    # docker cp options
    p.add_argument("-L", "--follow-link", action="store_true", help="Follow symbolic links when copying (default: False)")
    p.add_argument("-a", "--archive", action="store_true", help="Archive mode; copy directories recursively and preserve attributes (default: False)"   )

    p.add_argument("--log-level", default="INFO", help="Logging level (default: INFO)")
    p.add_argument("src", help="Source path with optional TYPE: prefix (e.g., 'satellite:/data/logs' or '/local/path')")
    p.add_argument("dest", help="Destination path with optional TYPE: prefix (e.g., 'user:/output' or '/local/path')")

    args = p.parse_args()
    log.setLevel(args.log_level.upper())

    src_spec = split_type_spec(args.src)
    dest_spec = split_type_spec(args.dest)

    if src_spec and dest_spec:
        log.error("❌ TYPE:PATH → TYPE:PATH is not supported")
        return 2
    if not src_spec and not dest_spec:
        log.error("❌ Either SRC or DEST must be TYPE:PATH")
        return 2

    if src_spec:
        node_type, node_path = src_spec
        direction = "FROM_NODE"
        host_path = Path(args.dest)
    else:
        node_type, node_path = dest_spec
        direction = "TO_NODE"

    # Etcd lookup
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

    nodes = get_nodes_by_type(etcd, node_type)
    if not nodes:
        log.error(f"❌ No nodes found with type '{node_type}'")
        return 1

    log.info(f"🔎 Found {len(nodes)} nodes of type '{node_type}': {', '.join(nodes)}")

    if direction == "FROM_NODE":
        if not host_path.exists() or not host_path.is_dir():
            log.error(f"❌ Destination must be an existing directory for TYPE:PATH pulls: {host_path}")
            return 2

        for node in nodes:
            with tempfile.TemporaryDirectory(prefix=f"{node}-") as tmpdir:
                tmp_path = Path(tmpdir)
                src = f"{node}:{node_path}"
                cmd = build_nsb_cp_cmd(args, src, tmpdir)
                log.info(f"📥 Pulling from node '{node}'")
                rc = subprocess.run(cmd).returncode
                if rc != 0:
                    log.error(f"❌ Copy failed for node '{node}' (exit={rc})")
                    return rc

                extracted = sorted(tmp_path.iterdir())
                if not extracted:
                    log.error(f"❌ No files extracted from node '{node}' path '{node_path}'")
                    return 1

                for item in extracted:
                    target = host_path / f"{node}_{item.name}"
                    safe_replace(item, target)
                    log.info(f"✅ Wrote {target}")

    else:
        for node in nodes:
            dest = f"{node}:{node_path}"
            cmd = build_nsb_cp_cmd(args, args.src, dest)
            log.info(f"📤 Pushing to node '{node}'")
            rc = subprocess.run(cmd).returncode
            if rc != 0:
                log.error(f"❌ Copy failed for node '{node}' (exit={rc})")
                return rc

    return 0


if __name__ == "__main__":
    sys.exit(main())
