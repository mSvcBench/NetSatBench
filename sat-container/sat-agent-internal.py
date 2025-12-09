#!/usr/bin/env python3
import os
import json
import time
import logging
import subprocess
import etcd3
import threading
import sys
import re

# ----------------------------
#  CONFIGURATION
# ----------------------------
ETCD_ENDPOINT = os.getenv("ETCD_ENDPOINT", "10.0.1.215:2379")
if ":" in ETCD_ENDPOINT:
    h, p = ETCD_ENDPOINT.split(":", 1)
    ETCD_HOST, ETCD_PORT = h, int(p)
else:
    ETCD_HOST, ETCD_PORT = ETCD_ENDPOINT, 2379

SAT_NAME = os.getenv("SAT_NAME")
KEY_LINKS = "/config/links"
KEY_RUN = f"/config/run/{SAT_NAME}"
UPDATE_LINK_SH = os.getenv("UPDATE_LINK_SH", "/agent/update-link-internal.sh")

logging.basicConfig(level="INFO", format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("sat-agent")

# ----------------------------
#  ETCD HELPERS
# ----------------------------
def get_etcd_client():
    while True:
        try:
            cli = etcd3.client(host=ETCD_HOST, port=ETCD_PORT)
            cli.get("/test_connection") # Dummy check
            return cli
        except Exception:
            log.warning("Waiting for Etcd connection...")
            time.sleep(5)

def get_my_config(cli):
    # 1. Try Satellite
    val, _ = cli.get(f"/config/satellites/{SAT_NAME}")
    if val: return json.loads(val.decode())
    # 2. Try User
    val, _ = cli.get(f"/config/users/{SAT_NAME}")
    if val: return json.loads(val.decode())
    return None

def get_links_data(cli):
    val, _ = cli.get(KEY_LINKS)
    if not val: return []
    try:
        return json.loads(val.decode())
    except: return []

def load_remote_ips(cli):
    ips = {}
    prefixes = ["/config/satellites/", "/config/users/"]
    for prefix in prefixes:
        for val, meta in cli.get_prefix(prefix):
            try:
                data = json.loads(val.decode())
                name = meta.key.decode().rsplit("/", 1)[-1]
                if "eth0_ip" in data:
                    ips[name] = data["eth0_ip"]
            except: pass
    return ips

# ----------------------------
#  IP & BRIDGE LOGIC
# ----------------------------
def register_my_ip(cli):
    try:
        # Get the IP
        cmd = "ip -4 addr show eth0 | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -n1"
        my_ip = subprocess.check_output(cmd, shell=True).decode().strip()
        
        #  VALIDATION CHECK
        # If IP is empty OR ends in .0 (network address), it's bad.
        if not my_ip or my_ip.endswith(".0"):
            log.warning(f"⚠️ Found invalid IP '{my_ip}'. Waiting for a real one...")
            return False

        # If we got here, the IP is good (e.g., 172.100.0.5)
        def update_if_needed(key):
            val, _ = cli.get(key)
            if val:
                data = json.loads(val.decode())
                if data.get("eth0_ip") != my_ip:
                    data["eth0_ip"] = my_ip
                    cli.put(key, json.dumps(data))
                    log.info(f"✅ Registered IP {my_ip} in {key}")
                return True
            return False

        if not update_if_needed(f"/config/satellites/{SAT_NAME}"):
            update_if_needed(f"/config/users/{SAT_NAME}")
            
        return True # Success!

    except Exception as e:
        log.error(f"Failed to register IP: {e}")
        return False

def ensure_interface_ip(iface, ip_cidr):
    if subprocess.run(f"ip link show {iface}", shell=True, stdout=subprocess.DEVNULL).returncode != 0:
        subprocess.run(f"ip link add name {iface} type bridge", shell=True)
        subprocess.run(f"ip link set dev {iface} up", shell=True)
    
    check = subprocess.run(f"ip addr show {iface} | grep 'inet {ip_cidr}'", shell=True, stdout=subprocess.DEVNULL)
    if check.returncode != 0:
        log.info(f"🛠️ Assigning IP {ip_cidr} to {iface}")
        subprocess.run(f"ip addr add {ip_cidr} dev {iface}", shell=True)

def apply_local_ips(cli):
    my_config = get_my_config(cli)
    if not my_config: return

    n_antennas = int(my_config.get("N_Antennas", 5))
    raw_cidr = my_config.get("sat_net_cidr")
    ip_vector = []

    if raw_cidr:
        # Use explicit config if present
        if isinstance(raw_cidr, list): ip_vector = raw_cidr
        elif isinstance(raw_cidr, str):
            prefix = raw_cidr.split('/')[0].rsplit('.', 1)[0]
            ip_vector = [f"{prefix}.{i}/32" for i in range(1, n_antennas + 1)]
    else:
        #  AUTO-GENERATE (Fallback for Ground Stations)
        match = re.search(r'(\d+)', SAT_NAME)
        if match:
            node_id = int(match.group(1))
            # Ground/Users get 200+ offset to avoid collisions
            if SAT_NAME.startswith("ground") or SAT_NAME.startswith("user"):
                octet = 200 + node_id
            else:
                octet = node_id
            
            # Keep octet valid (1-254)
            octet = octet % 254
            if octet == 0: octet = 1
            
            ip_vector = [f"192.168.{octet}.{i}/32" for i in range(1, n_antennas + 1)]
            log.info(f" Auto-generated IPs for {SAT_NAME}: {ip_vector}")

    for i, ip_cidr in enumerate(ip_vector):
        if i >= n_antennas: break 
        ensure_interface_ip(f"br{i+1}", ip_cidr)
    
    subprocess.run("ip link set dev lo up", shell=True)

def process_topology(cli):
    links = get_links_data(cli)
    remote_ips = load_remote_ips(cli)
    my_links = []
    expected = set()

    for l in links:
        ep1, ep2 = l.get("endpoint1"), l.get("endpoint2")
        if ep1 == SAT_NAME:
            expected.add(f"{ep2}_a{l['endpoint2_antenna']}")
            my_links.append(l)
        elif ep2 == SAT_NAME:
            expected.add(f"{ep1}_a{l['endpoint1_antenna']}")
            my_links.append(l)

    for l in my_links:
        ep1, ep2 = l["endpoint1"], l["endpoint2"]
        ip1, ip2 = remote_ips.get(ep1), remote_ips.get(ep2)

        # If the other node's IP is missing OR looks like a network address (.0), wait.
        if not ip1 or ip1.endswith(".0") or not ip2 or ip2.endswith(".0"):
             log.info(f"⏳ Waiting for valid remote IP for link {ep1}<->{ep2}...")
             continue 

        # If we get here, both IPs are valid (e.g., .4 and .5)
        subprocess.run(["/bin/bash", UPDATE_LINK_SH, ep1, str(l["endpoint1_antenna"]), ip1, ep2, str(l["endpoint2_antenna"]), ip2])

    # ... (rest of the function is fine)
    # Cleanup stale interfaces
    try:
        output = subprocess.check_output(["ip", "-o", "link", "show"], text=True)
        for line in output.splitlines():
            if "_a" in line:
                if_name = line.split(": ")[1].split("@")[0].strip()
                if if_name not in expected:
                    log.info(f"🗑️ Removing stale interface: {if_name}")
                    subprocess.run(["ip", "link", "del", if_name], check=False)
    except: pass

# ----------------------------
# RUNTIME EXECUTION LOGIC (THREAD 2)
# ----------------------------
def execute_commands(commands):
    if not commands: return
    log.info(f" Executing {len(commands)} runtime commands...")
    
    full_cmd = " && ".join(commands)
    
    # Run in the background so we don't block the network agent
    def run_shell():
        try:
            process = subprocess.Popen(
                f"/bin/bash -c '{full_cmd}'", 
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            for line in process.stdout:
                log.info(f"📤 [RUN] {line.strip()}")
            process.wait()
            if process.returncode != 0:
                log.error(f"❌ [RUN] Failed: {process.stderr.read()}")
            else:
                log.info("✅ [RUN] Finished successfully.")
        except Exception as e:
            log.error(f"❌ [RUN] Error: {e}")

    threading.Thread(target=run_shell, daemon=True).start()

# ----------------------------
# WATCHER THREADS
# ----------------------------
def watch_network_loop():
    """Thread 1: Watches Network Topology"""
    cli = get_etcd_client()
    # Initial setup
    register_my_ip(cli)
    apply_local_ips(cli)
    process_topology(cli)
    
    log.info("👀 [Network] Watching /config/links...")
    try:
        events, _ = cli.watch(KEY_LINKS)
        for event in events:
            log.info("🔔 Network Change Detected!")
            register_my_ip(cli)
            apply_local_ips(cli)
            process_topology(cli)
    except Exception as e:
        log.error(f"Network Watcher Failed: {e}")

def watch_runtime_loop():
    """Thread 2: Watches Runtime Commands"""
    cli = get_etcd_client()
    
    # Check initial commands
    val, _ = cli.get(KEY_RUN)
    if val: execute_commands(json.loads(val.decode()))

    log.info(f"👀 [Runtime] Watching {KEY_RUN}...")
    try:
        events, _ = cli.watch(KEY_RUN)
        for event in events:
            if event.value:
                log.info("🔔 New Command Received!")
                execute_commands(json.loads(event.value.decode()))
    except Exception as e:
        log.error(f"Runtime Watcher Failed: {e}")

# ----------------------------
# 🚀 MAIN
# ----------------------------
def main():
    log.info(f"🚀 Unified Agent Starting for {SAT_NAME}")

    # 1. Connect to Etcd immediately
    cli = get_etcd_client()

    # 2.  BLOCKING LOOP: Wait for a valid IP before doing ANYTHING else
    log.info("⏳ Verifying local network configuration...")
    while True:
        if register_my_ip(cli):
            break # IP is good, we can proceed!
        time.sleep(1) # IP was bad (or .0), wait 1s and try again

    # 3. Start Network Watcher (Now that we know our IP is safe)
    t_net = threading.Thread(target=watch_network_loop, daemon=True)
    t_net.start()

    # 4. Start Runtime Watcher
    t_run = threading.Thread(target=watch_runtime_loop, daemon=True)
    t_run.start()

    # Keep main thread alive
    while True:
        time.sleep(1)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("🛑 Agent stopped by user.")
        sys.exit(0)
