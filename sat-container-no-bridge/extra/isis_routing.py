#!/usr/bin/env python3
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
def routing_init(cli, data) -> tuple[str, bool]:
    try:
        node_name = data.get("node_name", "")
        area_id = data.get("area_id", "0001")
        loopback_ip_mask = data.get("loopback_ip_mask", ipaddress.ip_interface("127.0.0.1/32"))
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
        msg=f"✅ IS-IS configured (SysID: {sys_id}, AreaID: {area_id})"
        return msg, True 
    
    except Exception as e:
        msg=f"❌ Exception triggering IS-IS: {e}"
        return msg, False 

def routing_link_add(cli,link_data) -> tuple[str, bool]:
    vxlan_if = link_data.get("interface", "")
    cmd = [
            "vtysh",
            "-c", "conf t",
            "-c", f"interface {vxlan_if}",
            "-c", "ip router isis CORE",
            "-c", "isis network point-to-point",
            "-c", "end"
    ]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return f"✅ IS-IS enabled on {vxlan_if}", True 
    except Exception as e:
        return f"❌ Exception enabling IS-IS on {vxlan_if}: {e}", False
    
def routing_link_del(cli,link_data) -> tuple[str, bool]:
    vxlan_if = link_data.get("interface", "")
    cmd = [
            "vtysh",
            "-c", "conf t",
            "-c", f"no interface {vxlan_if}",
            "-c", "end"
    ]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return f"✅ IS-IS disabled on {vxlan_if}", True 
    except Exception as e:
        return f"❌ Exception disabling IS-IS on {vxlan_if}: {e}", False
