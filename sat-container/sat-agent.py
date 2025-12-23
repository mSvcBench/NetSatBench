#!/usr/bin/env python3
import ipaddress
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
#   CONFIGURATION
# ----------------------------
ETCD_ENDPOINT = os.getenv("ETCD_ENDPOINT", "127.0.0.1:2379")
if ":" in ETCD_ENDPOINT:
    h, p = ETCD_ENDPOINT.split(":", 1)
    ETCD_HOST, ETCD_PORT = h, int(p)
else:
    ETCD_HOST, ETCD_PORT = ETCD_ENDPOINT, 2379

SAT_NAME = os.getenv("SAT_NAME")

# KEYS
KEY_LINKS = f"/config/links/{SAT_NAME}"
KEY_L3 = "/config/L3-config"
KEY_RUN = f"/config/run/{SAT_NAME}"

LINK_ACTIONS_SH = "/app/link-actions.sh"
CONFIGURE_ISIS_SH = "/app/configure-isis.sh"

logging.basicConfig(level="INFO", format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("sat-agent")

# GLOBAL STATE
last_executed_cmd_raw = None
LINK_STATE_CACHE = {} # Only used for the initial sync

# ----------------------------
#   HELPERS
# ----------------------------
def get_etcd_client():
    log.info(f"📁 Connecting to Etcd at {ETCD_HOST}:{ETCD_PORT}...")
    while True:
        try:
            cli = etcd3.client(host=ETCD_HOST, port=ETCD_PORT)
            log.info(f"✅ Connected to Etcd at {ETCD_HOST}:{ETCD_PORT}.")
            return cli
        except:
            time.sleep(5)

def get_remote_ip(cli, node_name):
    val, _ = cli.get(f"/config/satellites/{node_name}")
    if not val: val, _ = cli.get(f"/config/users/{node_name}")
    if val:
        try: return json.loads(val.decode()).get("eth0_ip")
        except: pass
    return None

def get_l3_flags_values(cli):
    def _chk(k):
        val, _ = cli.get(f"{KEY_L3}/{k}")
        if not val: return True
        return val.decode().strip().replace('"', '').replace("'", "").lower() == "true"
    return _chk("ENABLE_TC"), _chk("ENABLE_ISIS")

def apply_isis_config(cli):

    try:
        val, _ = cli.get(f"/config/satellites/{SAT_NAME}")
        if not val: val, _ = cli.get(f"/config/users/{SAT_NAME}")
        if not val: val, _ = cli.get(f"/config/grounds/{SAT_NAME}")
        if not val: return
        my_conf = json.loads(val.decode())  
        available_ips = list(ipaddress.ip_network(my_conf.get("subnet_ip","")).hosts())
        n_ant = int(my_conf.get("n_antennas", 5))
    
        ## safety check, len of available_ips should be >= n_ant + 1 (the last one is for the loopback)
        if len(available_ips) < n_ant + 1:
            log.info(f"❌ Not enough IPs in subnet {my_conf.get('subnet_ip','')} for {n_ant} antennas. No ISIS will be configured.")
            return
        
        # Extract net_id from etcd key
        area_id = my_conf.get("/config/ISIS_AREA_ID","49.0001")   
        

        # Extract sys_id from subnet_ip Allocation
        subnet_cidr = my_conf.get("subnet_ip","")
        ip_part = subnet_cidr.split("/")[0] 
        octets = ip_part.split(".")
        # sys_id = last two octets zero-padded to 8 digits 
        sys_id = f"{int(octets[2]):04d}{int(octets[3]):04d}"
        
        n_ant = int(my_conf.get("n_antennas", 1))
        antennas = [str(i) for i in range(1, n_ant + 1)]
        
        cmd = [CONFIGURE_ISIS_SH, area_id, sys_id, subnet_cidr] + antennas
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log.error(f"❌ Exception triggering IS-IS: {e}")

def run(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.warning(f"⚠️ Command failed: {' '.join(cmd)}")
        log.warning(result.stderr.strip())

# ----------------------------
#   PART 1: INITIAL SETUP (Epoch 0)
# ----------------------------
def process_initial_topology(cli):
    """
    Reads /config/links and builds the initial world state.
    Uses 'add' action for everything found.
    """
    log.info("🏗️  Processing Initial Topology (Epoch 0)...")
    
    ## Process links add
    tc_flag, _ = get_l3_flags_values(cli)
    for value, meta in cli.get_prefix(KEY_LINKS):
        l = json.loads(value.decode())
        ep1, ep2 = l.get("endpoint1"), l.get("endpoint2")
        
        if ep1 != SAT_NAME and ep2 != SAT_NAME: 
            log.info(f"⚠️  Skipping initial link {ep1}<->{ep2} not relevant to this node.")
            continue

        ## Get remote IPs in a retry loop of 10 attempts
        ip1 = ip2 = None
        counter = 0
        while counter < 10:
            ip1 = get_remote_ip(cli, ep1)
            ip2 = get_remote_ip(cli, ep2)
            if ip1 and ip2: break
            time.sleep(2)
            counter += 1
        if not ip1 or not ip2: 
            log.warning(f"⚠️  Skipping initial link {ep1}<->{ep2} due to missing IPs.")
            continue
        
        vni = str(l.get("vni", "0"))
        if vni == "0":
            log.warning(f"⚠️  Skipping initial link {ep1}<->{ep2} due to missing VNI.")
            continue
        
        if tc_flag:
            bw = str(l.get("bw", ""))
            burst = str(l.get("burst", ""))
            latency = str(l.get("latency", ""))
        else:
            bw = burst = latency = ""

        # Just call ADD for everything in Epoch 0
        if ep1 == SAT_NAME:
            create_vxlan_link(
                vxlan_if=f"{ep2}_a{l['endpoint2_antenna']}",
                target_vni=vni,
                remote_ip=ip2,
                local_ip=ip1,
                target_bridge=f"br{l['endpoint1_antenna']}",
            ) 
            if tc_flag:
                apply_tc_settings(
                    vxlan_if=f"{ep2}_a{l['endpoint2_antenna']}",
                    bw=bw,
                    burst=burst,
                    latency=latency
                )
        elif ep2 == SAT_NAME:
            create_vxlan_link(
                vxlan_if=f"{ep1}_a{l['endpoint1_antenna']}",
                target_vni=vni,
                remote_ip=ip1,
                local_ip=ip2,
                target_bridge=f"br{l['endpoint2_antenna']}",
            )
            if tc_flag:
                apply_tc_settings(
                    vxlan_if=f"{ep1}_a{l['endpoint1_antenna']}",
                    bw=bw,
                    burst=burst,
                    latency=latency
                )
    
    ## Execute any pending runtime commands
    val, _ = cli.get(KEY_RUN)
    if val:
        log.info("▶️  Executing pending runtime commands after initial setup...")
        execute_commands(val.decode())
    
    ## Configure /etc/hosts entries for all known satellites/users
    log.info("📝 Updating /etc/hosts with known satellites and users...")
    prefix = "/config/etchosts/"
    for value, meta in cli.get_prefix(prefix):
        node_name = meta.key.decode().split('/')[-1]
        ip_addr = value.decode().strip()
        if ip_addr:
            try:
                # Check if entry already exists
                with open("/etc/hosts", "r") as f:
                    hosts_content = f.read()
                pattern = re.compile(rf"^{re.escape(ip_addr)}\s+{re.escape(node_name)}$", re.MULTILINE)
                if pattern.search(hosts_content):
                    continue
                # Append to /etc/hosts
                with open("/etc/hosts", "a") as f:
                    f.write(f"{ip_addr}\t{node_name}\n")
                log.info(f"✅ Added /etc/hosts entry: {ip_addr} {node_name}")
            except Exception as e:
                log.error(f"❌ Failed to update /etc/hosts for {node_name}: {e}")

# ----------------------------
#   PART 2: DYNAMIC ACTIONS (Epoch 1+)
# ----------------------------
def link_exists(ifname):
    return subprocess.run(
        ["ip", "link", "show", ifname],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0

def create_vxlan_link(
    vxlan_if,
    target_vni,
    remote_ip,
    local_ip,
    target_bridge,
    ):

        # ⛔ If already exists, do nothing
    if link_exists(vxlan_if):
        log.info(f"♻️  Link {vxlan_if} already exists, updating...")
        return
    
    log.info(f"🪢 Creating Link: {vxlan_if} (VNI: {target_vni})")

    run([
        "ip", "link", "add", vxlan_if,
        "type", "vxlan",
        "id", str(target_vni),
        "remote", remote_ip,
        "local", local_ip,
        "dev", "eth0",
        "dstport", "4789",
    ])

    run(["ip", "link", "set", vxlan_if, "mtu", "1350"])
    run(["ip", "link", "set", vxlan_if, "master", target_bridge])
    run(["ip", "link", "set", "dev", vxlan_if, "up"])

def delete_vxlan_link(
    vxlan_if):
    log.info(f"✂️   Deleteing Link: {vxlan_if}")

    run([
        "ip", "link", "del", vxlan_if
    ])

def apply_tc_settings(
    vxlan_if,
    bw,
    burst,
    latency):
    log.info(f"🎛️  Applying TC Settings on {vxlan_if}: BW={bw}, BURST={burst}, LATENCY={latency}"
    )
    # Clear existing qdisc
    run(["tc", "qdisc", "del", "dev", vxlan_if, "root"])
    # Apply new settings
    cmd = [
        "tc", "qdisc", "add", "dev", vxlan_if, "root", "tbf",
        "rate", f"{bw}",
        "burst", f"{burst}",
        "latency", f"{latency}"
    ]
    run(cmd)
def clean_tc_settings(
    vxlan_if):
    log.info(f"🎛️  Cleaning TC Settings on {vxlan_if}"
    )
    # Clear existing qdisc
    run(["tc", "qdisc", "del", "dev", vxlan_if, "root"])

def process_link_action(cli, event):
    try:
        tc_flag, _ = get_l3_flags_values(cli)
        if isinstance(event, etcd3.events.PutEvent):
                l = json.loads(event.value.decode())
                ep1, ep2 = l.get("endpoint1"), l.get("endpoint2")
                if ep1 != SAT_NAME and ep2 != SAT_NAME:
                    log.error(f"❌ Link action {ep1}<->{ep2} not relevant to this node.")
                    return

                ip1 = get_remote_ip(cli, ep1)
                ip2 = get_remote_ip(cli, ep2)
                if not ip1 or not ip2:
                    log.error(f"❌ Missing IPs for link action {ep1}<->{ep2}.")
                    return
                vni = str(l.get("vni", "0"))
                if ep1 == SAT_NAME:
                    create_vxlan_link(
                    vxlan_if=f"{ep2}_a{l['endpoint2_antenna']}",
                    target_vni=vni,
                    remote_ip=ip2,
                    local_ip=ip1,
                    target_bridge=f"br{l['endpoint1_antenna']}",
                ) 
                elif ep2 == SAT_NAME:
                    create_vxlan_link(
                        vxlan_if=f"{ep1}_a{l['endpoint1_antenna']}",
                        target_vni=vni,
                        remote_ip=ip1,
                        local_ip=ip2,
                        target_bridge=f"br{l['endpoint2_antenna']}",
                    )
                if tc_flag:
                    apply_tc_settings(
                        vxlan_if=f"{ep2}_a{l['endpoint2_antenna']}" if ep1 == SAT_NAME else f"{ep1}_a{l['endpoint1_antenna']}",
                        bw=str(l.get("bw", "")),
                        burst=str(l.get("burst", "")),
                        latency=str(l.get("latency", ""))
                    )

        elif isinstance(event, etcd3.events.DeleteEvent):
                key_str = event.key.decode()
                # Extract link info from the key, it is the last parto of the key after /
                iface = key_str.split('/')[-1]
                if tc_flag:
                    clean_tc_settings(iface)
                delete_vxlan_link(iface)
    except: 
        log.error("❌ Failed to parse link action.")
        return

# ----------------------------
#   WATCHERS
# ----------------------------
def watch_link_actions_loop():
    log.info("👀 Watching /config/links (Dynamic Events)...")
    cli = get_etcd_client()
    events_iterator, cancel = cli.watch_prefix(KEY_LINKS)
    for event in events_iterator:
        process_link_action(cli, event)

def watch_command_loop():
    log.info("👀 Watching Runtime Commands...")
    while True:
        try:
            cli = get_etcd_client()
            events, _ = cli.watch(KEY_RUN)
            for e in events:
                if e.value: execute_commands(e.value.decode())
        except: time.sleep(5)

def watch_etchosts_loop():
    log.info("👀 Watching /config/etchosts (Dynamic Events)...")
    cli = get_etcd_client()
    events_iterator, cancel = cli.watch_prefix("/config/etchosts/")
    for event in events_iterator:
        # If the event is a PutEvent, update /etc/hosts, otherwise if it is a delete event remove from /etc/hosts
        try:
            if isinstance(event, etcd3.events.PutEvent):
                node_name = event.key.decode().split('/')[-1]
                ip_addr = event.value.decode().strip()
                if ip_addr:
                    try:
                        # Check if entry already exists, in case remove and reinsert with current value
                        with open("/etc/hosts", "r") as f:
                            hosts_content = f.read()
                        pattern = re.compile(rf"^{re.escape(ip_addr)}\s+{re.escape(node_name)}$", re.MULTILINE)
                        if not pattern.search(hosts_content):
                            # Remove any existing entry for this node_name
                            hosts_content = re.sub(rf"^.*\s+{re.escape(node_name)}$\n", "", hosts_content, flags=re.MULTILINE)
                            # Append to /etc/hosts
                            with open("/etc/hosts", "w") as f:
                                f.write(hosts_content)
                                f.write(f"{ip_addr}\t{node_name}\n")
                            log.info(f"✅ Updated /etc/hosts entry: {ip_addr} {node_name}")
                    except Exception as e:
                        log.error(f"❌ Failed to update /etc/hosts for {node_name}: {e}")
            elif isinstance(event, etcd3.events.DeleteEvent):
                node_name = event.key.decode().split('/')[-1]
                try:
                    # Remove any existing entry for this node_name
                    with open("/etc/hosts", "r") as f:
                        hosts_content = f.read()
                    new_content = re.sub(rf"^.*\s+{re.escape(node_name)}$\n", "", hosts_content, flags=re.MULTILINE)
                    with open("/etc/hosts", "w") as f:
                        f.write(new_content)
                    log.info(f"✅ Removed /etc/hosts entry for: {node_name}")
                except Exception as e:
                    log.error(f"❌ Failed to remove /etc/hosts entry for {node_name}: {e}")
        except: 
            log.error("❌ Failed to process /config/etchosts event.")
            continue
        

def execute_commands(commands_raw_str):
    global last_executed_cmd_raw
    if not commands_raw_str or commands_raw_str == last_executed_cmd_raw: return
    try:
        commands = json.loads(commands_raw_str)
        log.info(f"▶️  Executing {len(commands)} runtime commands...")
        threading.Thread(target=lambda: subprocess.run(f"/bin/bash -c '{' && '.join(commands)}'", shell=True), daemon=True).start()
        last_executed_cmd_raw = commands_raw_str
    except: pass

def watch_satellites_loop():
    # Only useful if you need to detect new peers dynamically
    # For now, just a placeholder or you can implement if needed
    pass

def watch_users_loop():
    pass

# ----------------------------
#   INIT & MAIN
# ----------------------------
def register_my_ip(cli):
    try:
        cmd = "ip -4 addr show eth0 | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -n1"
        my_ip = subprocess.check_output(cmd, shell=True).decode().strip()
        if not my_ip or my_ip.endswith(".0"): return False
        
        def update_key(key):
            val, _ = cli.get(key)
            if not val: return False
            try:
                data = json.loads(val.decode())
                if data.get("eth0_ip") != my_ip:
                    data["eth0_ip"] = my_ip
                    cli.put(key, json.dumps(data))
                return True
            except: return False

        sat_found = update_key(f"/config/satellites/{SAT_NAME}")
        user_found = update_key(f"/config/users/{SAT_NAME}")
        return (sat_found or user_found)
    except: return False

def prepare_bridges(cli):
    val, _ = cli.get(f"/config/satellites/{SAT_NAME}")
    if not val: val, _ = cli.get(f"/config/users/{SAT_NAME}")
    if not val: val, _ = cli.get(f"/config/grounds/{SAT_NAME}")
    if not val: return
    my_config = json.loads(val.decode())
    available_ips = list(ipaddress.ip_network(my_config.get("subnet_ip","")).hosts())
    n_ant = int(my_config.get("n_antennas", 5))
    
    ## safety check, len of available_ips should be >= n_ant + 1 (the  last one is for the loopback used by routing protocols)
    if len(available_ips) < n_ant + 1:
        log.info(f"❌ Not enough IPs in subnet {my_config.get('subnet_ip','')} for {n_ant} antennas. No IPs will be assigned to bridges.")
        available_ips = []
    for i in range(1, n_ant + 1):
        br = f"br{i}"

        subprocess.run(f"ip link add {br} type bridge 2>/dev/null", shell=True)
        subprocess.run(f"ip link set {br} up", shell=True)
        
        if len(available_ips) >= i + 1:
            ip_addr = str(available_ips[i-1]) + "/32"
            subprocess.run(f"ip addr add {ip_addr} dev {br}", shell=True)
            log.info(f"✅ Bridge {br} created with IP {ip_addr}.")
            if i == 1:
                # Register last assigned IP in Etcd for this satellite/user
                cli.put(f"/config/etchosts/{SAT_NAME}", str(available_ips[i-1]))

def main():
    log.info(f"🚀 Sat Agent Starting for {SAT_NAME}")
    cli = get_etcd_client()
    
    # Bootstrapping
    ## Register my IP address in Etcd
    while True:
        if register_my_ip(cli): break
        time.sleep(2)
    
    ## Prepare internal bridges, one bridge for sat antenna, and possibly assign IP addresses
    prepare_bridges(cli)
    
    # 2. Initial Setup (Epoch 0)
    process_initial_topology(cli)
    
    # 3. L3 Routing Init
    _, isis_flag = get_l3_flags_values(cli)
    if isis_flag: apply_isis_config(cli)

    # 4. Start Event Loops
    threads = [
        threading.Thread(target=watch_link_actions_loop, daemon=True), # Dynamic
        threading.Thread(target=watch_command_loop, daemon=True)
        ,threading.Thread(target=watch_etchosts_loop, daemon=True)
    ]
    for t in threads: t.start()
    
    log.info(f"✅ All Watchers Started.")
    while True: time.sleep(1)

if __name__ == "__main__":
    try: main()
    except: sys.exit(0)