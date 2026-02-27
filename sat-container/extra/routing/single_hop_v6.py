#!/usr/bin/env python3
import json
import ipaddress
import subprocess
import threading
import time
from typing import Dict, List, Tuple

# ----------------------------
#   HELPERS
# ----------------------------

def run_cmd_capture(cmd: List[str]) -> str:
    res = subprocess.run(cmd, text=True, capture_output=True)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or f"Command failed: {' '.join(cmd)}")
    return res.stdout.strip()

def is_interface_up(interface: str) -> bool:
    try:
        return "UP" in run_cmd_capture(["ip", "link", "show", interface])
    except Exception:
        return False


def resolve_peer_link_local(peer_ipv6: str, interface: str, attempts: int = 5, wait_s: float = 0.05) -> str:
    def _lookup() -> str | None:
        out = run_cmd_capture(["ip", "-6", "neigh", "show", "to", peer_ipv6, "dev", interface])
        if out:
            for token in out.split():
                if token.lower().startswith("fe80:"):
                    return token

        out_dev = run_cmd_capture(["ip", "-6", "neigh", "show", "dev", interface])
        for line in out_dev.splitlines():
            if peer_ipv6 not in line:
                continue
            for token in line.split():
                if token.lower().startswith("fe80:"):
                    return token
        return None

    ll = _lookup()
    if ll:
        return ll

    for _ in range(attempts):
        subprocess.run(
            ["ping", "-6", "-c", "1", "-W", "1", "-I", interface, peer_ipv6],
            text=True,
            capture_output=True,
        )
        ll = _lookup()
        if ll:
            return ll
        time.sleep(wait_s)

    return None


# ----------------------------
#   MAIN FUNCTIONS
# ----------------------------
def init(etcd_client, node_name) -> tuple[str, bool]:
    try:
        val, _ = etcd_client.get(f"/config/nodes/{node_name}")
        my_config = json.loads(val.decode())
        l3_config = my_config.get("L3-config", {})
        if "cidr-v6" not in l3_config:
            msg=f" ❌ Configuration failed: No CIDR v6 assigned to node."
            return msg, False
        return f" ✅ Connected-only IPv6 routing initialized for node {node_name}", True
    except Exception as e:
        msg=f" ❌ Exception triggering connected-only-v6 routing: {e}"
        return msg, False 
    

def link_add(etcd_client, node_name, interface) -> tuple[str, bool]:
    # get IP address of the nodename from etc/hosts
    metric = 20
    try:
        # retrieve remote node name from interface name (assumes format vl_<name_remote>_<antenna_id>)
        remote_node = interface.split('_')[1]
        ip = run_cmd_capture(["grep", remote_node, "/etc/hosts"]).split()[0]
        if not ipaddress.ip_address(ip).version == 6:
            msg=f" ❌ Configuration failed: IP address {ip} for node {remote_node} is not IPv6."
            return msg, False
        # check interface up before adding route
        retry_count = 0        
        max_retries = 5
        up_flag = False
        while True:
             if is_interface_up(interface):
                up_flag = True
                break
             time.sleep(0.1)
             retry_count += 1
             if retry_count >= max_retries:
                break
        if not up_flag:
            msg=f" ❌ Configuration failed: Interface {interface} is not up."
            return msg, False
        ll_a = resolve_peer_link_local(ip, interface)
        if not ll_a:
            run_cmd_capture(["ip", "-6", "route", "replace", ip, "dev", interface, "metric", str(metric), "onlink"])
            msg=f" ⚠️ Connected-only IPv6 route to {remote_node} added on {interface} with metric {metric}, no link-local address available."
            return msg, True
        else:
            run_cmd_capture(["ip", "-6", "route", "replace", ip, "via", ll_a, "dev", interface, "metric", str(metric)])
            return f" ✅ Connected-only IPv6 route to {remote_node} added on {interface} with metric {metric}", True
    except Exception as e:        
        msg=f" ❌ Exception adding route for node {remote_node}: {e}"
        return msg, False   


    
def link_del(etcd_client, node_name, interface) -> tuple[str, bool]:
    # local route are removed automatically with the removal of the interface, so we can just return success here
    return f" ✅ Connected-only IPv6 route removed on {interface}", True
