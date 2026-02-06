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
log = logging.getLogger("constellation-unlink")


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
        prog="constellation-unlink",
        description="remove all links among nodes of the satellite system",
    )

    # Etcd
    p.add_argument("--etcd-host", default=os.getenv("ETCD_HOST", "127.0.0.1"))
    p.add_argument("--etcd-port", type=int, default=int(os.getenv("ETCD_PORT", "2379")))
    p.add_argument("--etcd-user", default=os.getenv("ETCD_USER"))
    p.add_argument("--etcd-password", default=os.getenv("ETCD_PASSWORD"))
    p.add_argument("--etcd-ca-cert", default=os.getenv("ETCD_CA_CERT"))


    p.add_argument("--log-level", default="INFO")


    args = p.parse_args()
    log.setLevel(args.log_level.upper())


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

    # remove from etcd all entries with prefix /config/links one by one
    try:
        log.info("✂️ Removing all links...")
        links = etcd.get_prefix("/config/links")
        for val, meta in links:
            key = meta.key.decode("utf-8")
            log.info(f"   ...Removing link config: {key}")
            etcd.delete(key)
    except Exception as e:
        log.error(f"❌ Failed to remove links: {e}")
        return 1
    log.info("✅ All links removed successfully.")


if __name__ == "__main__":
    sys.exit(main())
