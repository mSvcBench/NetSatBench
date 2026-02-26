#!/usr/bin/env python3
import argparse
import subprocess
import json
import socket
import sys
from typing import Any, Dict, List, Tuple
import logging
import os



def run_cmd_capture(cmd: List[str]) -> str:
    res = subprocess.run(cmd, text=True, capture_output=True)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or f"Command failed: {' '.join(cmd)}")
    return res.stdout.strip()


def run_cmd(cmd: List[str]) -> None:
    logging.debug("EXEC: %s", " ".join(cmd))
    res = subprocess.run(cmd, text=True, capture_output=True)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or f"Command failed: {' '.join(cmd)}")


def send_registration_request(grd_ipv6: str, grd_port: int,  usr_ipv6: str, callback_port: int, init_sat_ipv6: str) -> None:
    # add route to ground station via initial satellite (to ensure reachability for the registration request)
    run_cmd(["ip", "-6", "route", "replace", grd_ipv6, "via", init_sat_ipv6])
    msg: Dict[str, Any] = {
        "type": "registration_request",
        "user_id": os.environ["NODE_NAME"],
        "user_ipv6": usr_ipv6,
        "init_sat_ipv6": init_sat_ipv6,
        "callback_port": callback_port
    }
    data = json.dumps(msg).encode("utf-8")
    sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    sock.sendto(data, (grd_ipv6, grd_port))
    sock.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--grd-id", required=True, help="Name of configured ground station (e.g., grd1)")
    ap.add_argument("--grd-port", type=int, default=5005, help="UDP port on serving ground station (default: 5005)")
    ap.add_argument("--local-address", help='Local ipv6 address (Default: address found in /etc/hosts for the hostname)')
    ap.add_argument("--local-callback-port", type=int, default=5006, help='e.g., 5006 (callback port for handover_command)')
    ap.add_argument("--init-sat-id", required=True, help='Name of init satellite (e.g., sat2)')
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)

    if args.local_address is None:
        # Derive local IPv6 address from the loopback interface
        # cat /etc/hosts | grep $NODE_NAME
        local_ipv6 = run_cmd_capture(["grep", os.environ["NODE_NAME"], "/etc/hosts"]).split()[0]
        logging.debug("Derived local IPv6 address from /etc/hosts: %s", local_ipv6)
    else:
        local_ipv6 = args.local_address
        logging.debug("Using provided local IPv6 address: %s", local_ipv6)
    

    # resolve grd_address if it's a hostname
    if not ":" in args.grd_id:  # crude check for hostname vs IPv6
        try:
            grd_ipv6 = run_cmd_capture(["grep", args.grd_id, "/etc/hosts"]).split()[0]
            logging.debug("Resolved ground station address %s to %s", args.grd_id, grd_ipv6)
        except Exception as e:
            logging.error(f"Failed to resolve ground station address {args.grd_id}: {e}")
            sys.exit(1)
    
    # resolve new_sat if it's a hostname
    if not ":" in args.init_sat_id:  # crude check for hostname vs IPv6
        try:
            init_sat_ipv6 = run_cmd_capture(["grep", args.init_sat_id, "/etc/hosts"]).split()[0]
            logging.debug("Resolved initial satellite address %s to %s", args.init_sat_id, init_sat_ipv6)
        except Exception as e:
            logging.error(f"Failed to resolve initial satellite address {args.init_sat_id}: {e}")
            sys.exit(1)

    try:
        send_registration_request(grd_ipv6=grd_ipv6, grd_port=args.grd_port, usr_ipv6=local_ipv6, callback_port=args.local_callback_port, init_sat_ipv6=init_sat_ipv6)
        logging.info(f"✉️ Registration request sent to ground station {args.grd_id} for initial satellite {args.init_sat_id}")
    except Exception as e:
        logging.error(f"❌ failed to send registration request: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
