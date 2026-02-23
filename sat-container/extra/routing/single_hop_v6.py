#!/usr/bin/env python3
import json
import ipaddress
import subprocess
from time import time
from typing import List

# ----------------------------
#   HELPERS
# ----------------------------

def run_cmd_capture(cmd: List[str]) -> str:
    res = subprocess.run(cmd, text=True, capture_output=True)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or f"Command failed: {' '.join(cmd)}")
    return res.stdout.strip()
    
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
             if "UP" in run_cmd_capture(["ip", "link", "show", interface]):
                up_flag = True
                break
             time.sleep(0.1)
             retry_count += 1
             if retry_count >= max_retries:
                break
        if not up_flag:
            msg=f" ❌ Configuration failed: Interface {interface} is not up."
            return msg, False
        add_cmd = run_cmd_capture(["ip", "-6", "route", "replace", ip, "dev", interface, "metric", str(metric), "onlink"])
        return f" ✅ Connected-only IPv6 route to {remote_node} added on {interface} with metric {metric}", True
    except Exception as e:        
        msg=f" ❌ Exception adding route for node {remote_node}: {e}"
        return msg, False   


    
def link_del(etcd_client, node_name, interface) -> tuple[str, bool]:
    # local route are removed automatically with the removal of the interface, so we can just return success here
    return f" ✅ Connected-only IPv6 route removed on {interface}", True