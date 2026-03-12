#!/usr/bin/env python3
import argparse
import etcd3
import json
import os
import sys
import ipaddress
import logging
import copy
from itertools import islice
from typing import Any, Mapping
from pyparsing import Mapping
from scheduler import schedule_workers

logging.basicConfig(level="INFO", format="[%(levelname)s] %(message)s")
log = logging.getLogger("nsb-init")

# ==========================================
# ETCD CONNECTION
# ==========================================
def connect_etcd(etcd_host: str, etcd_port: int, etcd_user=None, etcd_password=None, etcd_ca_cert=None):
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

# ==========================================
 #Helper 
# ==========================================
def generate_ipv4_subnet(global_index: int, base_cidr: str, new_prefix: int = 30) -> str:
    """
    Generates sequential ipv4 /30 subnets from a parent network using the standard library.
    """
    try:
        # Create a network object from the input (e.g., 192.168.0.0/16)
        parent_network = ipaddress.ip_network(base_cidr)
        
        # The subnets() method returns a generator for all possible subnets of the specified prefix.
        # This is memory-efficient as it doesn't load all subnets into a list at once.
        subnets_generator = parent_network.subnets(new_prefix=new_prefix)
        
        # Use islice to jump directly to the desired index without a manual loop.
        # next() will retrieve the specific subnet at that index.
        target_subnet = next(islice(subnets_generator, global_index, None))
        return str(target_subnet)  
    except StopIteration:
        return "Error: Index out of range"
    except ValueError as e:
        return f"Error: {e}"

def generate_ipv6_subnet(global_index: int, base_cidr6: str, new_prefix: int = 64) -> str:
    """
    Generates sequential IPv6 subnets from a parent IPv6 network, memory-efficiently.
    Example: base /48 -> new_prefix /64 gives 2^(64-48)=65536 subnets.
    """
    try:
        parent_network = ipaddress.ip_network(base_cidr6)
        if parent_network.version != 6:
            return f"Error: base_cidr6 is not IPv6: {base_cidr6}"

        subnets_generator = parent_network.subnets(new_prefix=new_prefix)
        target_subnet = next(islice(subnets_generator, global_index, None))
        return str(target_subnet)
    except StopIteration:
        return "Error: Index out of range"
    except ValueError as e:
        return f"Error: {e}"

def merge_node_common_config(config_data: dict) -> dict:
    node_common_cfg = config_data.get("node-config-common", {})
    nodes_cfg = config_data.get("nodes", {})
    to_skip_keys = {"auto-assign-ips", "auto-assign-super-cidr"}

    merged_nodes_cfg = {}
    for name, node_cfg in nodes_cfg.items():
        merged_nodes_cfg[name] = deep_merge(node_common_cfg, node_cfg, to_skip_keys)

    return {**config_data, "nodes": merged_nodes_cfg}

def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any], to_skip_keys: set[str] = None) -> dict[str, Any]:
    """
    Recursively merge override into base.
    - Dict+dict: merge recursively
    - Otherwise: override wins
    Returns a NEW dict with NO shared nested dicts with inputs.
    """
    out: dict[str, Any] = {}

    # Start with base (deep-copied structure)
    for k, v in base.items():
        if to_skip_keys and k in to_skip_keys:
            continue
        if isinstance(v, dict):
            out[k] = deep_merge(v, {}, to_skip_keys)   # makes a fresh nested dict
        else:
            out[k] = copy.deepcopy(v)             # safe for lists/objects

    # Apply override
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = deep_merge(out[k], v, to_skip_keys)  # recursive merge
        else:
            out[k] = deep_merge(v, {}, to_skip_keys) if isinstance(v, dict) else copy.deepcopy(v)

    return out


# ==========================================
# PUSH CONFIG TO ETCD AND IP ASSIGNMENT
# ==========================================
def apply_config_to_etcd(etcd, config_data: dict):
   
    allowed_keys = [
        "nodes","node-config-common", "epoch-config"
    ]

    try:
        # init for auto-assign-ips (v4 + v6)
        super_cidr_vector_v4: dict[str, tuple[int, str]] = {}
        super_cidr_vector_v6: dict[str, tuple[int, str]] = {}
        node_common_cfg = config_data.get("node-config-common", {})
        auto_assign_ip = False
        if "L3-config" in node_common_cfg:
            l3_common_cfg = node_common_cfg["L3-config"]
            if "auto-assign-super-cidr" in l3_common_cfg:
                auto_assign_ip = True
                for supercidr_entry in l3_common_cfg["auto-assign-super-cidr"]:
                    match_type = supercidr_entry.get("matchType", "")
                    super_cidr = supercidr_entry.get("super-cidr", "")
                    super_cidr6 = supercidr_entry.get("super-cidr6", "")
                    if match_type and super_cidr:
                        super_cidr_vector_v4[match_type] = (0, super_cidr)
                    if match_type and super_cidr6:
                        super_cidr_vector_v6[match_type] = (0, super_cidr6)
        
        # upload config to etcd and auto-assign IPs if enabled
        for key, value in config_data.items():
            if key not in allowed_keys:
                log.warning(f"⚠️ Unexpected key '{key}', allowed keys are {allowed_keys}, skipping...")
                continue
            elif key in ["epoch-config"]:
                etcd.put(f"/config/{key}", json.dumps(value))
            elif key == "nodes":
                log.info(f"⚙️ Starting IP assignment process...")
                for name, node_cfg in value.items():
                    l3_cfg = node_cfg.get("L3-config", {})

                    # auto-assign IPs if enabled
                    if auto_assign_ip:
                        node_type = node_cfg.get("type", "any")

                        # ---- IPv4 ----
                        if "cidr" not in l3_cfg and node_type in super_cidr_vector_v4:
                            l3_cfg["cidr"] = generate_ipv4_subnet(
                                super_cidr_vector_v4[node_type][0],
                                super_cidr_vector_v4[node_type][1],
                                new_prefix=30  # /30 for point-to-point links, adjust as needed
                            )
                            super_cidr_vector_v4[node_type] = (
                                super_cidr_vector_v4[node_type][0] + 1,
                                super_cidr_vector_v4[node_type][1]
                            )

                        # ---- IPv6 ----
                        if "cidr-v6" not in l3_cfg and node_type in super_cidr_vector_v6:
                            l3_cfg["cidr-v6"] = generate_ipv6_subnet(
                                super_cidr_vector_v6[node_type][0],
                                super_cidr_vector_v6[node_type][1],
                                new_prefix=126  # /126 for point-to-point links, adjust as needed
                            )
                            super_cidr_vector_v6[node_type] = (
                                super_cidr_vector_v6[node_type][0] + 1,
                                super_cidr_vector_v6[node_type][1]
                            )

                    log.info(
                        f"    ➞ Assigned CIDR v4={l3_cfg.get('cidr', None)} "
                        f"v6={l3_cfg.get('cidr-v6', None)} to node {name} of type {node_cfg.get('type')}"
                    )

                    
                    etcd.put(
                        f"/config/{key}/{name}",
                        json.dumps(node_cfg),
                    )
                log.info(f"✅ IP assignment process completed.")
        log.info("👍 Successfully injected satellite system config to Etcd.")
        log.info("▶️ Proceed with nsb.py deploy to deploy node containers on workers.")

    except Exception as e:
        log.error(f"❌ Error in apply_config_to_etcd: {e}")
        sys.exit(1)

# ==========================================
# MAIN
# ==========================================
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inject satellite system configuration into Etcd"
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
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (default: INFO)",
    )
    args = parser.parse_args()
    log.setLevel(args.log_level.upper())

    # if ETCG host is localhost, suggest to set env variable and ask to continue
    if args.etcd_host in ["127.0.0.1", "localhost"]:
        log.warning("⚠️ Etcd host is set to localhost. Set ETCD_HOST to the actual Etcd server IP if remote workers are used.")
        cont = input("Do you want to continue? (y/n): ")
        if cont.lower() != 'y':
            log.info("Exiting as per user request.")
            sys.exit(0)

    etcd = connect_etcd(args.etcd_host, args.etcd_port, args.etcd_user, args.etcd_password, args.etcd_ca_cert)
    
    # check that workers exist in etcd otherwise ask to proceed with system-init-docker.py first
    try:
            existing_workers = etcd.get_prefix("/config/workers/")
            if not existing_workers:
                log.warning("⚠️  Workers do not found in Etcd under /config/workers/. This may indicate system init-docker.py has not been run.")
                cont = input("Do you want to continue with nsb-init? (y/n): ")
                if cont.lower() != 'y':
                    log.info("Exiting as per user request.")
                    sys.exit(0)
    except Exception as e:
        log.error(f"❌ Error checking existing nodes in Etcd: {e}")
        sys.exit(1)


    config_file = args.config

    # check that "/config/workers" exists in etcd and it is not void
    workers = list(etcd.get_prefix("/config/workers/"))
    if not workers:
        log.error("❌ '/config/workers' is missing or empty in Etcd, use system-init-docker.py first.")
        sys.exit(1)

    try:
        with open(config_file, "r", encoding="utf-8") as f:
            config_data = json.load(f)
    except Exception as e:
        log.error(f"❌ Failed to load file: {e}")
        return 1
    
    config_data = merge_node_common_config(config_data)
    scheduled_config = schedule_workers(config_data, etcd)
    apply_config_to_etcd(etcd, scheduled_config)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())