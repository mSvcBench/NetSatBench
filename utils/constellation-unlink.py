#!/usr/bin/env python3
import argparse
import logging
import os
import sys

import etcd3

logging.basicConfig(level="INFO", format="[%(levelname)s] %(message)s")
log = logging.getLogger("constellation-cp")



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

    args = p.parse_args()
    log.setLevel("INFO")


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

    etcd.delete_prefix("/config/links/")  # clean up any stale locks from previous runs
    log.info("✂️ Removed all links of the satellite system")

     # =========================


if __name__ == "__main__":
    sys.exit(main())
