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
from constellation_scheduler import schedule_workers

logging.basicConfig(level="INFO", format="[%(levelname)s] %(message)s")
log = logging.getLogger("constellation-init")

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


def merge_node_common_config(config_data: dict) -> dict:
    node_common_cfg = config_data.get("node-config-common", {})
    nodes_cfg = config_data.get("nodes", {})

    merged_nodes_cfg = {}
    for name, node_cfg in nodes_cfg.items():
        merged_nodes_cfg[name] = deep_merge(node_common_cfg, node_cfg)

    return {**config_data, "nodes": merged_nodes_cfg}

def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """
    Recursively merge override into base.
    - Dict+dict: merge recursively
    - Otherwise: override wins
    Returns a NEW dict with NO shared nested dicts with inputs.
    """
    out: dict[str, Any] = {}

    # Start with base (deep-copied structure)
    for k, v in base.items():
        if isinstance(v, dict):
            out[k] = deep_merge(v, {})   # makes a fresh nested dict
        else:
            out[k] = copy.deepcopy(v)             # safe for lists/objects

    # Apply override
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = deep_merge(v, {}) if isinstance(v, dict) else copy.deepcopy(v)

    return out


# ==========================================
# CONFIG INJECTION LOGIC
# ==========================================
def apply_config_to_etcd(etcd, config_data: dict):
   
    allowed_keys = [
        "nodes","node-config-common", "epoch-config"
    ]

    try:
        # init for auto-assign-ips
        super_cidr_vector: dict[str, tuple[int, str]] = {}
        super_cidr_vector["any"] = (0, "172.27.0.0/16")  # default base super cidr
        node_common_cfg = config_data.get("node-config-common", {})
        if "L3-config" in node_common_cfg:
            l3_common_cfg = node_common_cfg["L3-config"]
            if "auto-assign-super-cidr" in l3_common_cfg:
                for supercidr_entry in l3_common_cfg["auto-assign-super-cidr"]:
                    match_type = supercidr_entry.get("matchType","")
                    super_cidr = supercidr_entry.get("super-cidr","")
                    if match_type and super_cidr:
                        super_cidr_vector[match_type] = (0, super_cidr)
        # upload config to etcd
        for key, value in config_data.items():
            if key not in allowed_keys:
                log.warning(f"⚠️ Unexpected key '{key}', allowed keys are {allowed_keys}, skipping...")
                continue
            elif key in ["epoch-config"]:
                etcd.put(f"/config/{key}", json.dumps(value))
            elif key == "nodes":
                log.info(f"⚙️ Starting IP assignment process...")
                for name, node_cfg in value.items():
                    # merge with common config
                    l3_cfg = node_cfg.get("L3-config", {})
                    # auto-assign IPs if enabled
                    if l3_cfg.get("auto-assign-ips", False) is True:   
                        if "cidr" not in l3_cfg:
                            supercidr_type = node_cfg.get("type")
                            if supercidr_type not in super_cidr_vector:
                                supercidr_type = "any"
                            l3_cfg["cidr"] = generate_subnet(super_cidr_vector[supercidr_type][0], super_cidr_vector[supercidr_type][1])
                            super_cidr_vector[supercidr_type] = (super_cidr_vector[supercidr_type][0] + 1, super_cidr_vector[supercidr_type][1])    
                    log.info(f"    ➞ Assigned CIDR {l3_cfg.get('cidr', None)} to node {name} of type {node_cfg.get('type')}")
                    etcd.put(
                        f"/config/{key}/{name}",
                        json.dumps(node_cfg),
                    )
                log.info(f"✅ IP assignment process completed.")
        log.info("👍 Successfully injected satellite system config to Etcd.")
        log.info("▶️ Proceed with constellation-deploy.py to deploy node containers on workers.")

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
                cont = input("Do you want to continue with constellation init? (y/n): ")
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