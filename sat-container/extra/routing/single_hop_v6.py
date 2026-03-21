#!/usr/bin/env python3
import json
import ipaddress
import subprocess
import threading
import time
import shutil
from typing import Dict, List, Tuple, Optional

# ----------------------------
#   HELPERS
# ----------------------------

HAS_TIMEOUT = shutil.which("timeout") is not None
KEEPALIVE_LOCK = threading.Lock()
KEEPALIVE_STOPS: Dict[str, threading.Event] = {}
KEEPALIVE_THREADS: Dict[str, threading.Thread] = {}

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

def get_resolved_link_local(interface: str) -> Optional[str]:
    try:
        neigh = run_cmd_capture(["ip", "-6", "neigh", "show", "dev", interface])
        for line in neigh.splitlines():
            s = line.strip()
            # Require a resolved link-local neighbor entry.
            if s.startswith("fe80:") and "lladdr" in s and "FAILED" not in s and "INCOMPLETE" not in s:
                return s.split()[0]
        return None
    except Exception:
        return None

def wait_for_link_local_resolution(interface: str, retries: int = 30, delay_s: float = 1.0) -> Optional[str]:
    for _retry_attempt in range(retries):
        attempt_start_ts = time.monotonic()
        ll_addr = get_resolved_link_local(interface)
        if ll_addr is not None:
            return ll_addr
        # Trigger neighbor activity on-link without relying on a global unicast target.
        print(f" ⏳ Waiting for link-local address resolution on {interface}...attempt {_retry_attempt+1}/{retries}")
        subprocess.run(
            ["ping", "-6", "-n", "-c1", "-W1", "-I", interface, f"ff02::1%{interface}"],
            capture_output=True,
            text=True,
            check=False,
        )
        ping_done_ts = time.monotonic()
        ll_addr = get_resolved_link_local(interface)
        if ll_addr is not None:
            return ll_addr
        if _retry_attempt < retries - 1:
            remaining_delay = delay_s - (ping_done_ts - attempt_start_ts)
            if remaining_delay > 0:
                time.sleep(remaining_delay)
    return None

def _neighbor_keepalive_loop(interface: str, target_ip: str, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            if is_interface_up(interface):
                if HAS_TIMEOUT:
                    subprocess.run(
                        ["timeout", "0.3s", "ping", "-6", "-n", "-c1", "-W1", "-I", interface, target_ip],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                else:
                    subprocess.run(
                        ["ping", "-6", "-n", "-c1", "-W1", "-I", interface, target_ip],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
        except Exception:
            pass
        stop_event.wait(1)

def start_neighbor_keepalive(interface: str, target_ip: str) -> None:
    with KEEPALIVE_LOCK:
        old_stop = KEEPALIVE_STOPS.get(interface)
        old_thread = KEEPALIVE_THREADS.get(interface)
        if old_stop is not None:
            old_stop.set()
        if old_thread is not None and old_thread.is_alive():
            old_thread.join(timeout=0.5)

        stop_event = threading.Event()
        t = threading.Thread(
            target=_neighbor_keepalive_loop,
            args=(interface, target_ip, stop_event),
            daemon=True,
        )
        KEEPALIVE_STOPS[interface] = stop_event
        KEEPALIVE_THREADS[interface] = t
        t.start()

def stop_neighbor_keepalive(interface: str) -> None:
    with KEEPALIVE_LOCK:
        stop_event = KEEPALIVE_STOPS.pop(interface, None)
        t = KEEPALIVE_THREADS.pop(interface, None)
    if stop_event is not None:
        stop_event.set()
    if t is not None and t.is_alive():
        t.join(timeout=0.5)

def refresh_hosts_ipv6_cache() -> None:
    global hosts_ipv6_cache
    parsed: Dict[str, str] = {}
    with open("/etc/hosts", "r", encoding="utf-8", errors="ignore") as hosts_file:
        for raw_line in hosts_file:
            line = raw_line.split("#", 1)[0].strip()
            if not line:
                continue
            fields = line.split()
            if len(fields) < 2:
                continue
            ip = fields[0]
            if ":" not in ip:
                continue
            for alias in fields[1:]:
                parsed[alias] = ip
    hosts_ipv6_cache = parsed

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
        refresh_hosts_ipv6_cache()
        return f" ✅ IPv6 routing initialized for node {node_name}", True
    except Exception as e:
        msg=f" ❌ Exception triggering connected-only-v6 routing: {e}"
        return msg, False 
    

def link_add(etcd_client, node_name, interface) -> tuple[str, bool]:
    # get IP address of the nodename from etc/hosts
    metric = 20
    try:
        # retrieve remote node name from interface name (assumes format vl_<name_remote>_<antenna_id>)
        remote_node = interface.split('_')[1]
        ip = hosts_ipv6_cache.get(remote_node)
        if ip is None:
            # 3 attempts to repopulate hosts cache in case of recent changes (e.g., due to dynamic DNS updates or recent node additions)
            for _ in range(3):
                refresh_hosts_ipv6_cache()
                ip = hosts_ipv6_cache.get(remote_node)
                if ip is not None:
                    break
                time.sleep(1)  # brief pause before refresh to allow for any pending updates to /etc/hosts
        if ip is None:
            msg=f" ❌ Configuration failed: No IPv6 address found for node {remote_node} in /etc/hosts."
            return msg, False
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
        link_local = wait_for_link_local_resolution(interface)
        if link_local is None:
            msg=f" ❌ Configuration failed: Link-local address for {remote_node} on {interface} not resolved."
            return msg, False
        run_cmd_capture(["ip", "-6", "route", "replace", ip, "via", link_local, "dev", interface, "metric", str(metric)])
        start_neighbor_keepalive(interface, ip)
        msg=f" ✅ IPv6 route to {remote_node} added on {interface} with metric {metric}"
        return msg, True
    except Exception as e:        
        msg=f" ❌ Exception adding route for node {remote_node}: {e}"
        return msg, False   


    
def link_del(etcd_client, node_name, interface) -> tuple[str, bool]:
    stop_neighbor_keepalive(interface)
    # local route are removed automatically with the removal of the interface, so we can just return success here
    return f" ✅ IPv6 route removed on {interface}", True
