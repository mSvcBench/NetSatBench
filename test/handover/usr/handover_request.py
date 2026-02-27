#!/usr/bin/env python3
import argparse
import subprocess
import json
import socket
import sys
import time
from typing import Any, Dict, List, Tuple
import logging
import os


def run_cmd_capture(cmd: List[str]) -> str:
    res = subprocess.run(cmd, text=True, capture_output=True)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or f"Command failed: {' '.join(cmd)}")
    return res.stdout.strip()


def run_cmd(cmd: List[str], dry_run: bool) -> None:
    if dry_run:
        logging.info("DRY-RUN: %s", " ".join(cmd))
        return
    logging.debug("EXEC: %s", " ".join(cmd))
    res = subprocess.run(cmd, text=True, capture_output=True)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or f"Command failed: {' '.join(cmd)}")


def send_handover_request(grd_ipv6: str, port: int,  user_ipv6: str, callback_port: int, new_sat_ipv6: str) -> None:
    try:
        txid = str(int(time.time() * 1000)) # simple nonce txid for correlation (current timestamp in ms)
        msg: Dict[str, Any] = {
            "type": "handover_request",
            "user_id": os.environ["NODE_NAME"],
            "user_ipv6": user_ipv6,
            "new_sat_ipv6": new_sat_ipv6,
            "callback_port": callback_port,
            "txid": txid
        }
        data = json.dumps(msg).encode("utf-8")
        sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        sock.sendto(data, (grd_ipv6, port))
        sock.close()
    except Exception as e:
        raise                    


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--grd-id", required=True, help="Name of ground station (e.g., grd1)")
    ap.add_argument("--grd-port", type=int, default=5005, help="UDP port on serving ground station (default: 5005)")
    ap.add_argument("--local-address", help='Local ipv6 address (Default: address found in /etc/hosts for the hostname)')
    ap.add_argument("--local-callback-port", type=int, default=5006, help='e.g., 5006 (callback port for handover_command)')
    ap.add_argument("--new-sat-id", required=True, help='Name of new sat (e.g., sat2)')
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
    
    grd_ipv6 = args.grd_id
    # resolve grd_ipv6 if it's a hostname
    if not ":" in args.grd_id:  # crude check for hostname vs IPv6
        try:
            grd_ipv6 = run_cmd_capture(["grep", args.grd_id, "/etc/hosts"]).split()[0]
            logging.debug("Resolved ground station IPv6 address %s to %s", args.grd_id, grd_ipv6)
        except Exception as e:
            logging.error(f"Failed to resolve ground station IPv6 address {args.grd_id}: {e}")
            sys.exit(1)
    
    new_sat_ipv6 = args.new_sat_id
    # resolve new_sat_ipv6 if it's a hostname
    if not ":" in args.new_sat_id:  # crude check for hostname vs IPv6
        try:
            new_sat_ipv6 = run_cmd_capture(["grep", args.new_sat_id, "/etc/hosts"]).split()[0]
            logging.debug("Resolved new satellite IPv6 address %s to %s", args.new_sat_id, new_sat_ipv6)
        except Exception as e:
            logging.error(f"Failed to resolve new satellite IPv6 address {args.new_sat_id}: {e}")
            sys.exit(1)
            
    try:
        send_handover_request(grd_ipv6=grd_ipv6, port=args.grd_port, user_ipv6=local_ipv6, callback_port=args.local_callback_port, new_sat_ipv6=new_sat_ipv6)
        logging.info(f"✉️ Handover request sent to ground station {args.grd_id} for new satellite {args.new_sat_id}")
    except Exception as e:
        logging.error(f"❌ failed to send handover request: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
