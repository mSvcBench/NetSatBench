#!/usr/bin/env python3
import time
import etcd3
import json
import ipaddress
import hashlib
from pathlib import Path
import subprocess
from typing import Mapping, Optional
from extra.routing.rutils import replace_placeholders_in_file

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

def pick_last_usable_ip(net: ipaddress._BaseNetwork):
    """Return a single stable IP inside `net` (works for IPv4 and IPv6)."""
    if net is None:
        return None

    if net.num_addresses == 1:
        return net.network_address

    # IPv4: avoid network/broadcast for prefixes <= /30
    if net.version == 4 and net.prefixlen <= 30:
        return net.broadcast_address - 1

    # IPv6: highest address is usable (no broadcast concept)
    return net.network_address + (net.num_addresses - 1)


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

        v4_net = ipaddress.ip_network(l3_config.get("cidr",""), strict=False)
        loopback_ip = pick_last_usable_ip(v4_net)
        if loopback_ip is None:
            msg = "  ❌ IS-IS configuration failed: Unable to derive loopback IP from CIDR."
            return msg, False
        loopback_mask = l3_config.get("cidr","").split('/')[1] if '/' in l3_config.get("cidr","") else '30'
        loopback_ip_mask = f"{loopback_ip}/{loopback_mask}"
        # Extract sys_id from node name 
        sys_id = derive_sysid_from_string(node_name)
        replace_placeholders_in_file(
            "/app/extra/routing/isis-template.conf",
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

        ## Configure advertisement of default route if specified in config
        if l3_config.get("routing-metadata", {}).get("advertize-default-route", False):
            cmd = ["ip", "route", "show", "default"]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                msg=f"  ❌ IS-IS default route advertisement failed: Unable to determine local default route."
                return msg, False
            default_gw = result.stdout.strip().split()[2]
            cmd = [
                "vtysh",
                "-c", "conf t",
                "-c", f"ip route 0.0.0.0/1 {default_gw}",
                "-c", f"ip route 128.0.0.0/1 {default_gw}",
                "-c", "router isis CORE",
                "-c", "redistribute ipv4 static level-2",
                "-c", "end"
            ]
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            ## enable NAT on eth0 interface for outgoing traffic
            cmd = ["iptables", "-t", "nat", "-A", "POSTROUTING", "-o", "eth0", "-j", "MASQUERADE"]
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        msg=f"  ✅ IS-IS configured (SysID: {sys_id}, AreaID: {area_id}, Default route advertisement: {'enabled' if l3_config.get('routing-metadata', {}).get('advertize-default-route', False) else 'disabled'})"
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