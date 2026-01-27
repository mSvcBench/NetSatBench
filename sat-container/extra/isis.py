#!/usr/bin/env python3
import time
import etcd3
import json
import ipaddress
import hashlib
from pathlib import Path
import subprocess
from typing import Mapping, Optional
from extra.rutils import replace_placeholders_in_file

# ----------------------------
#   HELPERS
# ----------------------------

def derive_sysid_from_string(value: str) -> str:
    """
    Derive an 8-digit IS-IS system-id from an arbitrary string
    using a cryptographic hash (deterministic, stable).
    """
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    num = int.from_bytes(digest[:4], byteorder="big")  # 32 bits
    return f"{num % 10**8:08d}"


# ----------------------------
#   MAIN FUNCTIONS
# ----------------------------
def init(etcd_client, node_name) -> tuple[str, bool]:
    try:
        val, _ = etcd_client.get(f"/config/nodes/{node_name}")
        my_config = json.loads(val.decode())
        l3_config = my_config.get("L3-config", {})
        if "cidr" not in l3_config:
            msg=f"  ❌ IS-IS configuration failed: No CIDR assigned to node."
            return msg, False
        area_id = l3_config.get("metadata", {}).get("isis-area-id","0001")  # default area ID
        available_ips = list(ipaddress.ip_network(l3_config.get("cidr","")).hosts())
        loopback_ip = available_ips[-1] if available_ips else ipaddress.ip_address("127.0.0.1")
        loopback_mask = l3_config.get("cidr","").split('/')[1] if '/' in l3_config.get("cidr","") else '30'
        loopback_ip_mask = f"{loopback_ip}/{loopback_mask}"
        # Extract sys_id from node name 
        sys_id = derive_sysid_from_string(node_name)
        replace_placeholders_in_file(
            "/app/extra/isis-template.conf",
            {
                "hostname": node_name,
                "lo_iface": "lo",
                "lo_ip": str(loopback_ip_mask),
                "isis_name": "CORE",
                "area_id": area_id,
                "part1": sys_id[:4],
                "part2": sys_id[4:],
            },
            "/etc/frr/frr.conf"
        )
        cmd = ["service", "frr", "restart"]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)  # Allow some time for FRR to restart
        msg=f"  ✅ IS-IS configured (SysID: {sys_id}, AreaID: {area_id})"
        return msg, True 
    
    except Exception as e:
        msg=f"  ❌ Exception triggering IS-IS: {e}"
        return msg, False 

def link_add(etcd_client, node_name, interface) -> tuple[str, bool]:
    cmd = [
            "vtysh",
            "-c", "conf t",
            "-c", f"interface {interface}",
            "-c", "ip router isis CORE",
            "-c", "isis network point-to-point",
            "-c", "end"
    ]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return f"  ✅ IS-IS enabled on {interface}", True 
    except Exception as e:
        return f"  ❌ Exception enabling IS-IS on {interface}: {e}", False
    
def link_del(etcd_client, node_name, interface) -> tuple[str, bool]:
    cmd = [
            "vtysh",
            "-c", "conf t",
            "-c", f"no interface {interface}",
            "-c", "end"
    ]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return f"  ✅ IS-IS disabled on {interface}", True 
    except Exception as e:
        return f"  ❌ Exception disabling IS-IS on {interface}: {e}", False