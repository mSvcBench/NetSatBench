#!/usr/bin/env python3

# ==========================================
# MAIN
# ==========================================
import argparse
import json
import logging
from time import sleep

logging.basicConfig(level="INFO", format="[%(levelname)s] %(message)s")
log = logging.getLogger("nsb-meshing")

def main() -> int:
    parser = argparse.ArgumentParser(description="Add links among any object of a specific type on a specific epoc file")
    parser.add_argument("-c", "--config", default=None, help="Path to the JSON emulation configuration file (e.g., sat-config.json)",)
    parser.add_argument("-e", "--epoc-file", default=None, help="Path to the specific epoc file to process.")
    parser.add_argument("--delay", default="1ms", help="Delay to apply for the created links (default: 1ms).")
    parser.add_argument("--rate", default="200mbps", help="Rate of links to create (default: 200mbps).")
    parser.add_argument("--loss", type=int, default=0, help="Loss percentage for links (default: 0).")
    parser.add_argument("--type", default="gateway", help="Comma separated value of typed of object to mesh (default: gateway).")
    parser.add_argument("--natv6", action="store_true", help="Whether to also add NAT IPv6 rules for the meshed nodes (default: False).")
    parser.add_argument("--natv4", action="store_true", help="Whether to also add NAT IPv4 rules for the meshed nodes (default: False).")
    parser.add_argument("--log-level", default="INFO", help="Logging level (default: INFO).")
    parser.add_argument("--dry-run", action="store_true", help="Print the links that would be added without modifying the epoc file.")

    args = parser.parse_args()
    log.setLevel(args.log_level.upper())
    
    if args.config is None:
        # print help message and exit if no config file provided since we need the config file to find the nodes to mesh
        parser.print_help()
        exit(0)
    if args.epoc_file is None:
        # print help message and exit if no config file provided since we need the config file to find the nodes to mesh
        parser.print_help()
        exit(0)
    
    # Load the emulation configuration
    with open(args.config, "r") as f:
        config = json.load(f)
    # parse node to mesh based on type and json format, e.g
    # "nodes": {
    # "grd1": {
    #     "type": "gateway"
    # }},
    node_to_mesh = []
    for node_name, node_cfg in config.get("nodes", {}).items():
        if node_cfg.get("type") == args.type:
            node_to_mesh.append(node_name)
    log.info(f"Found {len(node_to_mesh)} nodes of type {args.type} to mesh")
    # Load the epoc file    with open(args.epoc_file, "r") as f:
    with open(args.epoc_file, "r") as f:
        epoc = json.load(f)
    # Add links among all nodes of the specified type, e.g.
    # "links-add": [
    #   {
    #     "endpoint1": "grd1",
    #     "endpoint2": "grd2",
    #     "rate": "200mbit",
    #     "loss":0,
    #     "delay": "1ms"
    #   }],
    #   "run": {
    #    "grd1": [
    #        "ip6tables -t nat -A POSTROUTING -o vl_grd2_1 -j MASQUERADE"
    #    ],
    #   }
    for i in range(len(node_to_mesh)):
        for j in range(i+1, len(node_to_mesh)):
            node1 = node_to_mesh[i]
            node2 = node_to_mesh[j]
            epoc["links-add"].append({
                "endpoint1": node1,
                "endpoint2": node2,
                "rate": args.rate,
                "loss": args.loss,
                "delay": args.delay
            })
            log.info(f"Added link between {node1} and {node2} with rate={args.rate} loss={args.loss} delay={args.delay}")
            if args.natv6:
                if "run" not in epoc:
                    epoc["run"] = {}
                if node1 not in epoc["run"]:
                    epoc["run"][node1] = []
                if node2 not in epoc["run"]:
                    epoc["run"][node2] = []

                epoc["run"][node1].append(f"ip6tables -t nat -D POSTROUTING -o vl_{node2}_1 -j MASQUERADE")
                epoc["run"][node2].append(f"ip6tables -t nat -D POSTROUTING -o vl_{node1}_1 -j MASQUERADE")    
                epoc["run"][node1].append(f"ip6tables -t nat -A POSTROUTING -o vl_{node2}_1 -j MASQUERADE")
                epoc["run"][node2].append(f"ip6tables -t nat -A POSTROUTING -o vl_{node1}_1 -j MASQUERADE")
                log.info(f"Added IPv6 NAT rules for {node1} and {node2} to masquerade outgoing traffic on the new link")
            if args.natv4:
                if "run" not in epoc:
                    epoc["run"] = {}
                if node1 not in epoc["run"]:
                    epoc["run"][node1] = []
                if node2 not in epoc["run"]:
                    epoc["run"][node2] = []
                epoc["run"][node1].append(f"iptables -t nat -D POSTROUTING -o vl_{node2}_1 -j MASQUERADE")
                epoc["run"][node2].append(f"iptables -t nat -D POSTROUTING -o vl_{node1}_1 -j MASQUERADE")
                epoc["run"][node1].append(f"iptables -t nat -A POSTROUTING -o vl_{node2}_1 -j MASQUERADE")
                epoc["run"][node2].append(f"iptables -t nat -A POSTROUTING -o vl_{node1}_1 -j MASQUERADE")
                log.info(f"Added IPv4 NAT rules for {node1} and {node2} to masquerade outgoing traffic on the new link")
    if args.dry_run:
        log.info("Dry run mode: not writing to epoc file")
        print(json.dumps(epoc, indent=2))
    else:
        with open(args.epoc_file, "w") as f:
            json.dump(epoc, f, indent=2)


if __name__ == "__main__":
    main()