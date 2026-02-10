#!/usr/bin/env python3
import time
import json
import ipaddress
import hashlib
import subprocess
from extra.rutils import replace_placeholders_in_file


# ----------------------------
#   HELPERS
# ----------------------------

def derive_sysid_from_string(value: str) -> str:
    """Deterministically derive an 8-digit IS-IS system-id from an arbitrary string."""
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    num = int.from_bytes(digest[:4], byteorder="big")  # 32 bits
    return f"{num % 10**8:08d}"


def _parse_cidr(cidr: str):
    if not cidr:
        return None
    try:
        return ipaddress.ip_network(cidr, strict=False)
    except Exception:
        return None


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
    """Initialize IPv6-only IS-IS in FRR."""
    try:
        val, _ = etcd_client.get(f"/config/nodes/{node_name}")
        my_config = json.loads(val.decode())
        l3_config = my_config.get("L3-config", {})

        # sat-agent uses `cidr-v6`
        cidr_v6 = l3_config.get("cidr-v6")
        if not cidr_v6:
            msg = "  ❌ IS-ISv6 configuration failed: No IPv6 CIDR assigned to node (missing L3-config.cidr-v6)."
            return msg, False

        area_id = l3_config.get("metadata", {}).get("isis-area-id", "0001")

        v6_net = _parse_cidr(cidr_v6)
        loopback_ip = pick_last_usable_ip(v6_net)
        if loopback_ip is None:
            msg = "  ❌ IS-ISv6 configuration failed: Unable to derive loopback IPv6 from cidr-v6."
            return msg, False
        loopback_mask = l3_config.get("cidr-v6","").split('/')[1] if '/' in l3_config.get("cidr-v6","") else '128'
        loopback_ip_mask = f"{loopback_ip}/{loopback_mask}"
        
        # Extract sys_id from node name (deterministic, stable)
        sys_id = derive_sysid_from_string(node_name)

        replace_placeholders_in_file(
            "/app/extra/isisv6-template.conf",
            {
                "hostname": node_name,
                "lo_iface": "lo",
                "lo_ip": str(loopback_ip_mask),
                "isis_name": "CORE",
                "area_id": area_id,
                "part1": sys_id[:4],
                "part2": sys_id[4:],
            },
            "/etc/frr/frr.conf",
        )

        subprocess.Popen(["service", "frr", "restart"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)

        # Optional: advertise a "default" for IPv6 by splitting it in two halves.
        if l3_config.get("routing-metadata", {}).get("advertize-default-route", False):
            # Example output: "default via fe80::1 dev eth0 metric 1024"
            result = subprocess.run(["ip", "-6", "route", "show", "default"], capture_output=True, text=True)
            if result.returncode != 0 or not result.stdout.strip():
                msg = "  ❌ IS-ISv6 default route advertisement failed: Unable to determine local IPv6 default route."
                return msg, False

            parts = result.stdout.strip().split()
            try:
                via_idx = parts.index("via")
                default_gw = parts[via_idx + 1]
            except Exception:
                msg = f"  ❌ IS-ISv6 default route advertisement failed: Unexpected output: {result.stdout.strip()}"
                return msg, False

            cmd = [
                "vtysh",
                "-c", "conf t",
                # split ::/0 into halves (same idea as your IPv4 :: default workaround)
                "-c", f"ipv6 route ::/1 {default_gw}",
                "-c", f"ipv6 route 8000::/1 {default_gw}",
                "-c", "router isis CORE",
                "-c", "redistribute ipv6 static level-2",
                "-c", "end",
            ]
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        msg = (
            f"  ✅ IS-ISv6 configured (SysID: {sys_id}, AreaID: {area_id}, "
            f"Default route advertisement: {'enabled' if l3_config.get('routing-metadata', {}).get('advertize-default-route', False) else 'disabled'})"
        )
        return msg, True

    except Exception as e:
        msg = f"  ❌ Exception triggering IS-ISv6: {e}"
        return msg, False


def link_add(etcd_client, node_name, interface) -> tuple[str, bool]:
    """Enable IS-IS IPv6 address-family on an interface."""
    cmd = [
        "vtysh",
        "-c", "conf t",
        "-c", f"interface {interface}",
        "-c", "ipv6 router isis CORE",
        "-c", "isis network point-to-point",
        "-c", "end",
    ]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return f"  ✅ IS-ISv6 enabled on {interface}", True
    except Exception as e:
        return f"  ❌ Exception enabling IS-ISv6 on {interface}: {e}", False


def link_del(etcd_client, node_name, interface) -> tuple[str, bool]:
    """Remove interface stanza from running config (mirrors isis.py behavior)."""
    cmd = [
        "vtysh",
        "-c", "conf t",
        "-c", f"no interface {interface}",
        "-c", "end",
    ]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return f"  ✅ IS-ISv6 disabled on {interface}", True
    except Exception as e:
        return f"  ❌ Exception disabling IS-ISv6 on {interface}: {e}", False
