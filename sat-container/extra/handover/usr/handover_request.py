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


def send_handover_request(grd_addr: str, port: int,  usr_prefix: str, callback_port: int, new_sat: str) -> None:
    txid = str(int(time.time() * 1000)) # simple nonce txid for correlation (current timestamp in ms)
    msg: Dict[str, Any] = {
        "type": "handover_request",
        "user_id": os.environ["HOSTNAME"],
        "usr_prefix": usr_prefix,
        "new_sat": new_sat,
        "callback_port": callback_port,
        "txid": txid
    }
    data = json.dumps(msg).encode("utf-8")
    sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    sock.sendto(data, (grd_addr, port))
    sock.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--grd", required=True, help="IPv6 address of the serving ground station or name resolvable via /etc/hosts (e.g., grd1 or 2001:db8:101::1)")
    ap.add_argument("--grd-port", type=int, default=5005, help="UDP port on serving ground station (default: 5005)")
    ap.add_argument("--local-address", help='Local ipv6 address (Default: address found in /etc/hosts for the hostname)')
    ap.add_argument("--local-callback-port", type=int, default=5006, help='e.g., 5006 (callback port for handover_command)')
    ap.add_argument("--new-sat", required=True, help='IPv6 of the new access satellite or name resolvable via /etc/hosts (e.g., sat2 or 2001:db8:100::3)')
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)

    if args.local_address is None:
        # Derive local IPv6 address from the loopback interface
        # cat /etc/hosts | grep $HOSTNAME
        local_ipv6 = run_cmd_capture(["grep", os.environ["HOSTNAME"], "/etc/hosts"]).split()[0]
        logging.debug("Derived local IPv6 address from /etc/hosts: %s", local_ipv6)
    else:
        local_ipv6 = args.local_address
        logging.debug("Using provided local IPv6 address: %s", local_ipv6)
    
    # resolve grd_address if it's a hostname
    if not ":" in args.grd:  # crude check for hostname vs IPv6
        try:
            grd_addr = run_cmd_capture(["grep", args.grd, "/etc/hosts"]).split()[0]
            logging.debug("Resolved ground station address %s to %s", args.grd, grd_addr)
        except Exception as e:
            logging.error(f"Failed to resolve ground station address {args.grd}: {e}")
            sys.exit(1)
    
    # resolve new_sat if it's a hostname
    if not ":" in args.new_sat:  # crude check for hostname vs IPv6
        try:
            new_sat = run_cmd_capture(["grep", args.new_sat, "/etc/hosts"]).split()[0]
            logging.debug("Resolved new satellite address %s to %s", args.new_sat, new_sat)
        except Exception as e:
            logging.error(f"Failed to resolve new satellite address {args.new_sat}: {e}")
            sys.exit(1)
            
    try:
        send_handover_request(grd_addr=grd_addr, port=args.grd_port, usr_prefix=local_ipv6, callback_port=args.local_callback_port, new_sat=new_sat)
        logging.info(f"✉️ Handover request sent to ground station {args.grd} for new satellite {args.new_sat}")
    except Exception as e:
        logging.error(f"❌ failed to send handover request: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
