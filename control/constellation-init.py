#!/usr/bin/env python3
import argparse
import etcd3
import json
import os
import sys
import ipaddress
from itertools import islice
from constellation_scheduler import schedule_workers

# ==========================================
# ETCD CONNECTION
# ==========================================
def connect_etcd(etcd_host: str, etcd_port: int, etcd_user=None, etcd_password=None, etcd_ca_cert=None):
    try:
        print(f"📁 Connecting to Etcd at {etcd_host}:{etcd_port}...")
        if etcd_user and etcd_password:
            return etcd3.client(host=etcd_host, port=etcd_port, user=etcd_user, password=etcd_password, ca_cert=etcd_ca_cert)
        else:
            return etcd3.client(host=etcd_host, port=etcd_port)
    except Exception as e:
        print(f"❌ Failed to initialize Etcd client: {e}")
        sys.exit(1)

# ==========================================
 #Helper 
# ==========================================
def generate_subnet(global_index: int, base_cidr: str) -> str:
    """
    Generates sequential /30 subnets from a parent network using the standard library.
    """
    try:
        # Create a network object from the input (e.g., 192.168.0.0/16)
        parent_network = ipaddress.ip_network(base_cidr)
        
        # The subnets() method returns a generator for all possible subnets of the specified prefix.
        # This is memory-efficient as it doesn't load all subnets into a list at once.
        subnets_generator = parent_network.subnets(new_prefix=30)
        
        # Use islice to jump directly to the desired index without a manual loop.
        # next() will retrieve the specific subnet at that index.
        target_subnet = next(islice(subnets_generator, global_index, None))
        return str(target_subnet)  
    except StopIteration:
        return "Error: Index out of range"
    except ValueError as e:
        return f"Error: {e}"


# ==========================================
# CONFIG INJECTION LOGIC
# ==========================================
def apply_config_to_etcd(etcd, config_data: dict):
   
    allowed_keys = [
        "satellites", "users", "grounds",
        "L3-config-common", "workers", "epoch-config"
    ]

    try:
        global_subnet_counter = 0
        for key, value in config_data.items():
            if key not in allowed_keys:
                print(f"⚠️ Unexpected key '{key}', skipping...")
                continue
            
            if key == "L3-config-common":
                etcd.put(f"/config/{key}", json.dumps(value))
            elif key in ["epoch-config"]:
                etcd.put(f"/config/{key}", json.dumps(value))
            elif key == "workers":
                for name, node_cfg in value.items():
                    etcd.put(f"/config/workers/{name}", json.dumps(node_cfg))
            
            elif key in ["satellites", "users", "grounds"]:
                for name, node_cfg in value.items():
                    my_l3_cfg = config_data.get("L3-config-common", {}).copy()
                    local_l3_node_cfg = node_cfg.get("L3-config", {})
                    for key_l3, val_l3 in local_l3_node_cfg.items():
                        my_l3_cfg[key_l3] = val_l3
                    
                    if my_l3_cfg.get("auto-assign-ips", False) is True:
                        if "subnet_cidr" not in node_cfg:
                            base_cidr = my_l3_cfg.get("auto-assign-cidr", "192.168.0.0/16")
                            node_cfg["subnet_cidr"] = generate_subnet(global_subnet_counter, base_cidr)
                            global_subnet_counter += 1
                    
                    etcd.put(
                        f"/config/{key}/{name}",
                        json.dumps(node_cfg),
                    )

        print(f"✅ Successfully applied constellation config to Etcd.")

    except Exception as e:
        print(f"❌ Error in apply_config_to_etcd: {e}")
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
        default="examples/10nodes-sched/sat-config.json",
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
    parser.add_argument(
        "--etcd-ca-cert",
        default=os.getenv("ETCD_CA_CERT", None ),
        help="Path to Etcd CA certificate (default: env ETCD_CA_CERT or None)",
    )
    args = parser.parse_args()
    
    etcd = connect_etcd(args.etcd_host, args.etcd_port, args.etcd_user, args.etcd_password)
    config_file = args.config

    try:
        with open(config_file, "r", encoding="utf-8") as f:
            config_data = json.load(f)
    except Exception as e:
        print(f"❌ Failed to load file: {e}")
        return 1

    scheduled_config = schedule_workers(config_data, etcd)
    apply_config_to_etcd(etcd, scheduled_config)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())