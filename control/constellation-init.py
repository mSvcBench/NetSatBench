#!/usr/bin/env python3
import argparse
import etcd3
import json
import os
import sys

# ==========================================
# ETCD CONNECTION
# ==========================================
def connect_etcd(etcd_host: str, etcd_port: int, etcd_user = None, etcd_password = None):
    try:
        print(f"📁 Connecting to Etcd at {etcd_host}:{etcd_port}...")
        if etcd_user and etcd_password:
            return etcd3.client(host=etcd_host, port=etcd_port, user=etcd_user, password=etcd_password)
        else:
            return etcd3.client(host=etcd_host, port=etcd_port)
    except Exception as e:
        print(f"❌ Failed to initialize Etcd client: {e}")
        sys.exit(1)

# ==========================================
# CONFIG INJECTION LOGIC
# ==========================================
def apply_config_to_etcd(etcd, filename: str):
    allowed_keys = [
        "satellites",
        "users",
        "grounds",
        "L3-config-common",
        "workers",
        "epoch-config",
    ]

    try:
        with open(filename, "r", encoding="utf-8") as f:
            config = json.load(f)

        for key, value in config.items():
            if key not in allowed_keys:
                print(f"❌ [{filename}] Unexpected key '{key}', skipping...")
                continue
            if key == "L3-config-common":
                etcd.put(f"/config/{key}", json.dumps(value))
            elif key in ["epoch-config"]:
                etcd.put(f"/config/{key}", json.dumps(value))
            elif key in ["satellites", "users", "grounds"]:
                for name, node_cfg in value.items():
                    etcd.put(
                        f"/config/{key}/{name}",
                        json.dumps(node_cfg),
                    )

        print(f"✅ Successfully applied constellation config from {filename} to Etcd.")

    except FileNotFoundError:
        print(f"❌ Error: File '{filename}' not found.")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"❌ Error: Failed to parse JSON in '{filename}': {e}")
        sys.exit(1)


# ==========================================
# MAIN
# ==========================================
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inject constellation configuration into Etcd"
    )
    parser.add_argument(
        "-c", "--config",
        default="sat-config.json",
        required=False,
        help="Path to the JSON emulation configuration file (e.g., sat-config.json)",
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

    args = parser.parse_args()

    etcd = connect_etcd(args.etcd_host, args.etcd_port, args.etcd_user, args.etcd_password)
    apply_config_to_etcd(etcd, args.config)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
