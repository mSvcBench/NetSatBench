#!/usr/bin/env python3
import argparse
import copy
import etcd3
import ipaddress
import json
import logging
import os
import sys
from itertools import islice
from typing import Any, Mapping

logging.basicConfig(level="INFO", format="[%(levelname)s] %(message)s")
log = logging.getLogger("nsb-init")
scheduler_impl = None

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
# Helper 
# ==========================================

def get_prefix_data(etcd, prefix: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for value, metadata in etcd.get_prefix(prefix):
        key = metadata.key.decode('utf-8').split('/')[-1]
        try:
            data[key] = json.loads(value.decode('utf-8'))
        except json.JSONDecodeError:
            log.warning(f"⚠️ Warning: Could not parse JSON for key {key} under {prefix}")
    return data

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

def get_nested_value(data: dict[str, Any], dotted_key: str) -> Any:
    value: Any = data
    for key in dotted_key.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
        if value is None:
            return None
    return value

def normalize_node_common_entries(node_common_cfg: Any) -> list[dict[str, Any]]:
    if isinstance(node_common_cfg, list):
        normalized_entries: list[dict[str, Any]] = []
        for index, entry in enumerate(node_common_cfg):
            if not isinstance(entry, dict):
                log.warning(f"⚠️ Skipping node-config-common entry #{index}: expected object, got {type(entry).__name__}")
                continue
            match_key = entry.get("match-key")
            match_value = entry.get("match-value")
            config_common = entry.get("config-common")
            if not match_key or "match-value" not in entry or not isinstance(config_common, dict):
                log.warning(
                    f"⚠️ Skipping node-config-common entry #{index}: requires match-key, match-value, and object config-common"
                )
                continue
            normalized_entries.append({
                "match-key": match_key,
                "match-value": match_value,
                "config-common": config_common,
            })
        return normalized_entries

    if isinstance(node_common_cfg, dict):
        return [{
            "match-key": "any",
            "match-value": True,
            "config-common": node_common_cfg,
        }]

    if node_common_cfg:
        log.warning(f"⚠️ Unsupported node-config-common type {type(node_common_cfg).__name__}, ignoring it.")
    return []

def node_matches_common_entry(node_cfg: dict[str, Any], common_entry: dict[str, Any]) -> bool:
    if common_entry["match-key"] == "any":
        return True
    return get_nested_value(node_cfg, common_entry["match-key"]) == common_entry["match-value"]

def build_super_cidr_rule_sets(common_entry: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    super_cidr_rules_v4: list[dict[str, Any]] = []
    super_cidr_rules_v6: list[dict[str, Any]] = []

    l3_common_cfg = common_entry["config-common"].get("L3-config", {})
    if not isinstance(l3_common_cfg, dict):
        return super_cidr_rules_v4, super_cidr_rules_v6

    for index, supercidr_entry in enumerate(l3_common_cfg.get("auto-assign-super-cidr", [])):
        if not isinstance(supercidr_entry, dict):
            log.warning(
                f"⚠️ Skipping auto-assign-super-cidr entry #{index} in node-config-common rule "
                f"{common_entry.get('match-key')}={common_entry.get('match-value')}: expected object"
            )
            continue
        match_key = supercidr_entry.get("match-key", "")
        match_value = supercidr_entry.get("match-value", None)
        super_cidr = supercidr_entry.get("super-cidr", "")
        super_cidr6 = supercidr_entry.get("super-cidr6", "")
        if match_key and match_value is not None and super_cidr:
            super_cidr_rules_v4.append({
                "match-key": match_key,
                "match-value": match_value,
                "next-index": 0,
                "super-cidr": super_cidr,
            })
        if match_key and match_value is not None and super_cidr6:
            super_cidr_rules_v6.append({
                "match-key": match_key,
                "match-value": match_value,
                "next-index": 0,
                "super-cidr6": super_cidr6,
            })

    return super_cidr_rules_v4, super_cidr_rules_v6

def merge_node_common_config(config_data: dict) -> dict:
    node_common_entries = normalize_node_common_entries(config_data.get("node-config-common", {}))
    nodes_cfg = config_data.get("nodes", {})
    to_skip_keys = {"auto-assign-ips", "auto-assign-super-cidr"}

    merged_nodes_cfg = {}
    for name, node_cfg in nodes_cfg.items():
        merged_cfg: dict[str, Any] = {}
        for common_entry in node_common_entries:
            if node_matches_common_entry(node_cfg, common_entry):
                merged_cfg = deep_merge(merged_cfg, common_entry["config-common"], to_skip_keys)
        merged_nodes_cfg[name] = deep_merge(merged_cfg, node_cfg, to_skip_keys)

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

def get_full_config_path(config_file: str) -> str:
    base, ext = os.path.splitext(config_file)
    if not ext:
        ext = ".json"
    return f"{base}-full{ext}"

def write_full_config(config_file: str, sat_config_data: dict) -> None:
    full_config_path = get_full_config_path(config_file)
    try:
        with open(full_config_path, "w", encoding="utf-8") as f:
            json.dump(sat_config_data, f, indent=2)
            f.write("\n")
        log.info(f"📝 Wrote expanded full satellite config to {full_config_path}")
    except Exception as e:
        log.error(f"❌ Failed to write full config file {full_config_path}: {e}")
        sys.exit(1)


# ==========================================
# PUSH CONFIG TO ETCD
# ==========================================
def apply_config_to_etcd(etcd, sat_config_data: dict, worker_config_data: dict) -> None:
   
    try:
        allowed_keys = {"epoch-config", "nodes", "node-config-common"}
        for key, cfg in sat_config_data.items():
            if key not in allowed_keys:
                log.warning(f"⚠️ Unexpected key '{key}', allowed keys are {allowed_keys}, skipping...")
                continue
            elif key in ["epoch-config"]:
                etcd.put(f"/config/{key}", json.dumps(cfg))
            elif key == "nodes":
                for node_name, node_cfg in cfg.items():
                    # Write to Etcd under /config/nodes/{node_name}
                    key = f"/config/nodes/{node_name}"
                    etcd.put(key, json.dumps(node_cfg))
        # update worker info on etcd
        for worker_name, worker_cfg in worker_config_data.items():
            # Write to Etcd under /config/workers/{worker_name}
            key = f"/config/workers/{worker_name}"
            etcd.put(key, json.dumps(worker_cfg))
        log.info("👍 Successfully injected satellite system config to Etcd.")
        log.info("▶️ Proceed with nsb.py deploy to deploy node containers on workers.")
    except Exception as e:
        log.error(f"❌ Error in apply_config_to_etcd: {e}")
        sys.exit(1)

# ==========================================
#  IP ADDRESSING LOGIC
# ==========================================
def auto_ip_addressing(sat_config_data: dict) -> dict:
    sat_config_data_new = sat_config_data.copy()
    try:
        node_common_entries = normalize_node_common_entries(sat_config_data_new.get("node-config-common", {}))
        common_entry_rule_sets: list[dict[str, Any]] = []
        auto_assign_ip = False
        for common_entry in node_common_entries:
            l3_common_cfg = common_entry["config-common"].get("L3-config", {})
            if not isinstance(l3_common_cfg, dict):
                continue
            super_cidr_rules_v4, super_cidr_rules_v6 = build_super_cidr_rule_sets(common_entry)
            common_entry_rule_sets.append({
                "common-entry": common_entry,
                "auto-assign-ips": bool(l3_common_cfg.get("auto-assign-ips")),
                "super-cidr-rules-v4": super_cidr_rules_v4,
                "super-cidr-rules-v6": super_cidr_rules_v6,
            })
            auto_assign_ip = auto_assign_ip or bool(l3_common_cfg.get("auto-assign-ips"))
        
        # upload config to etcd and auto-assign IPs if enabled
        for key, value in sat_config_data_new.items():
            if key == "nodes":
                log.info(f"⚙️ Starting IP assignment process...")
                for name, node_cfg in value.items():
                    l3_cfg = node_cfg.get("L3-config", {})

                    # auto-assign IPs if enabled
                    matched_string_v4 = "none"
                    matched_string_v6 = "none"
                    if auto_assign_ip:
                        selected_common_entry_rule_set = next(
                            (
                                entry_rule_set
                                for entry_rule_set in common_entry_rule_sets
                                if node_matches_common_entry(node_cfg, entry_rule_set["common-entry"])
                            ),
                            None,
                        )
                        matched_rule_v4 = None
                        matched_rule_v6 = None
                        if selected_common_entry_rule_set and selected_common_entry_rule_set["auto-assign-ips"]:
                            for rule in selected_common_entry_rule_set["super-cidr-rules-v4"]:
                                if get_nested_value(node_cfg, rule["match-key"]) == rule["match-value"]:
                                    matched_rule_v4 = rule
                                    break

                            for rule in selected_common_entry_rule_set["super-cidr-rules-v6"]:
                                if get_nested_value(node_cfg, rule["match-key"]) == rule["match-value"]:
                                    matched_rule_v6 = rule
                                    break

                        # ---- IPv4 ----
                        if "cidr" not in l3_cfg and matched_rule_v4:
                            l3_cfg["cidr"] = generate_ipv4_subnet(
                                matched_rule_v4["next-index"],
                                matched_rule_v4["super-cidr"],
                                new_prefix=30  # /30 for point-to-point links, adjust as needed
                            )
                            matched_rule_v4["next-index"] += 1
                            matched_string_v4 = f"{matched_rule_v4['match-key']}={matched_rule_v4['match-value']}"

                        # ---- IPv6 ----
                        if "cidr-v6" not in l3_cfg and matched_rule_v6:
                            l3_cfg["cidr-v6"] = generate_ipv6_subnet(
                                matched_rule_v6["next-index"],
                                matched_rule_v6["super-cidr6"],
                                new_prefix=126  # /126 for point-to-point links, adjust as needed
                            )
                            matched_rule_v6["next-index"] += 1
                            matched_string_v6 = f"{matched_rule_v6['match-key']}={matched_rule_v6['match-value']}"

                    log.info(
                        f"    ➞ Assigned CIDR v4={l3_cfg.get('cidr', None)} "
                        f"v6={l3_cfg.get('cidr-v6', None)} "
                        f"to node {name}, matched rule v4: {matched_string_v4}, matched rule v6: {matched_string_v6}"
                    )

                log.info(f"✅ IP assignment process completed.")
                return sat_config_data_new
        return sat_config_data_new
    except Exception as e:
        log.error(f"❌ Error in auto_ip_addressing: {e}")
        sys.exit(1)



# ==========================================
# MAIN
# ==========================================
def main() -> int:
    global scheduler_impl
    parser = argparse.ArgumentParser(
        description="Inject satellite system configuration into Etcd"
    )
    parser.add_argument(
        "-c", "--config",
        default="examples/10nodes-sched/sat-config.json",
        required=False,
        help="Path to the JSON emulation configuration file (e.g., sat-config.json)",
    )
    parser.add_argument("-s","--sched",type=str,default="base", help="Scheduling algorithm to use (default: base)")
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
    parser.add_argument(
        "--write-full-config",
        action="store_true",
        help="Write an expanded '<config>-full.json' file after merge, scheduling, and IP assignment.",
    )
    args = parser.parse_args()
    log.setLevel(args.log_level.upper())

    # if ETCD host is localhost, suggest to set env variable and ask to continue
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

    # check nodes exists in etcd, if yes, ask to proceed with nsb-init or exit
    try:        
        existing_nodes = etcd.get_prefix("/config/nodes/")
        if len(list(existing_nodes))>0:
            log.warning("⚠️  Nodes already exist in Etcd under /config/nodes/. This may indicate nsb-init has been run before.")
            cont = input("Do you want to continue and overwrite existing nodes? (y/n): ")
            if cont.lower() != 'y':
                log.info("Exiting as per user request.")
                sys.exit(0)
    except Exception as e:
        log.error(f"❌ Error checking existing nodes in Etcd: {e}")
        sys.exit(1) 

    config_file = args.config

    # check that "/config/workers" exists in etcd and it is not void
    worker_confing_data = get_prefix_data(etcd, "/config/workers/")
    if not worker_confing_data:
        log.error("❌ '/config/workers' is missing or empty in Etcd, use system-init-docker.py first.")
        sys.exit(1)

    try:
        with open(config_file, "r", encoding="utf-8") as f:
            sat_config_data = json.load(f)
    except Exception as e:
        log.error(f"❌ Failed to load file: {e}")
        return 1
    
    if args.sched == "base":
        import scheduler as scheduler_impl
    elif args.sched == "metis":
        import scheduler_metis as scheduler_impl
    else:
        log.error(f"❌ Invalid scheduler specified: {args.sched}. Use 'base' or 'metis'.")
        sys.exit(1)
    sat_config_data = merge_node_common_config(sat_config_data)
    sat_config_data, worker_confing_data = scheduler_impl.schedule_workers(sat_config_data, worker_confing_data)
    sat_config_data = auto_ip_addressing(sat_config_data)
    if args.write_full_config:
        write_full_config(config_file, sat_config_data)
    apply_config_to_etcd(etcd, sat_config_data, worker_confing_data)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
