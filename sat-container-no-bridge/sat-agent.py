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
import hashlib
#from extra.isis_routing import routing_init, routing_link_add, routing_link_del   

# ----------------------------
#   CONFIGURATION
# ----------------------------
ETCD_ENDPOINT = os.getenv("ETCD_ENDPOINT", "127.0.0.1:2379")
if ":" in ETCD_ENDPOINT:
    h, p = ETCD_ENDPOINT.split(":", 1)
    ETCD_HOST, ETCD_PORT = h, int(p)
else:
    ETCD_HOST, ETCD_PORT = ETCD_ENDPOINT, 2379

my_node_name = os.getenv("SAT_NAME")

# KEYS
KEY_LINKS = f"/config/links/{my_node_name}_"
KEY_L3 = "/config/L3-config-common"
KEY_RUN = f"/config/run/{my_node_name}_"

logging.basicConfig(level="INFO", format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("sat-agent")

# GLOBAL STATE
last_executed_cmd_raw = None
LINK_STATE_CACHE = {} # Only used for the initial sync
l3_flags = None
my_config = None
cli = None
routing = None

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
    if not val: val, _ = cli.get(f"/config/grounds/{node_name}")
    if val:
        try: return json.loads(val.decode()).get("eth0_ip")
        except: pass
    return None

def get_l3_flags_values(cli):
    l3_flags={}
    val, _ = cli.get(KEY_L3)
    if val:
        try:
            l3_flags = json.loads(val.decode())
        except:
            log.error("❌ Failed to parse L3 flags from Etcd.")
    return l3_flags

def run(cmd, log_errors=True):
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 and log_errors:
        log.warning(f"⚠️ Command failed: {' '.join(cmd)}")
        log.warning(result.stderr.strip())
    return result

def build_netem_opts(l):
    """
    Build netem options dictionary from link descriptor `l`,
    including only non-empty values.
    """
    netem_opts = {}

    # One-argument netem options
    for key in [
        "rate",
        "loss",
        "duplicate",
        "corrupt",
    ]:
        val = l.get(key)
        if val not in (None, "", []):
            netem_opts[key] = val

    # Delay can be multi-argument (delay + jitter + distribution)
    delay = l.get("delay")
    if delay not in (None, "", []):
        delay_opts = [delay]

        jitter = l.get("jitter")
        if jitter not in (None, "", []):
            delay_opts.append(jitter)

            distribution = l.get("distribution")
            if distribution not in (None, "", []):
                delay_opts.extend(["distribution", distribution])

        netem_opts["delay"] = delay_opts

    # Reordering can be multi-argument
    reorder = l.get("reorder")
    if reorder not in (None, "", []):
        gap = l.get("gap")
        if gap not in (None, "", []):
            netem_opts["reorder"] = [reorder, gap]
        else:
            netem_opts["reorder"] = reorder

    return netem_opts



def derive_sysid_from_string(value: str) -> str:
    """
    Derive an 8-digit IS-IS system-id from an arbitrary string
    using a cryptographic hash (deterministic, stable).
    """
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    num = int.from_bytes(digest[:4], byteorder="big")  # 32 bits
    return f"{num % 10**8:08d}"


# ----------------------------
#   PART 1: INITIAL SETUP
# ----------------------------
def process_initial_topology(cli):
    """
    Reads /config/links and builds the initial world state.
    Uses 'add' action for everything found.
    """
    log.info("🏗️  Processing Initial Topology ...")
    
    ## Process links add
    tc_flag = l3_flags.get("enable-netem", True)
    for value, meta in cli.get_prefix(KEY_LINKS):
        l = json.loads(value.decode())
        ep1, ep2 = l.get("endpoint1"), l.get("endpoint2")
        
        if ep1 != my_node_name and ep2 != my_node_name: 
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
            netem_opts = build_netem_opts(l)

        key_str = meta.key.decode()
        # Extract interface name from the key, it is the last part of the key after /
        vxlan_if = key_str.split('/')[-1]
        # ADD for found link
        if ep1 == my_node_name:
            remote_ip = ip2
            local_ip = ip1
        else:
            remote_ip = ip1
            local_ip = ip2
        create_vxlan_link(
            vxlan_if=vxlan_if,
            target_vni=vni,
            remote_ip=remote_ip,
            local_ip=local_ip,
        ) 
        if tc_flag:
            if netem_opts:
                apply_tc_settings(
                    vxlan_if=vxlan_if,
                    netem_opts=netem_opts
                )
            else:
                log.info(f"🎛️  No netem options defined for {vxlan_if}, skipping tc")
    
    ## Execute any pending runtime commands
    val, _ = cli.get(KEY_RUN)
    if val:
        log.info("▶️  Executing pending runtime commands after initial setup...")
        execute_commands(val.decode())
    
    ## Configure /etc/hosts entries for all known satellites/grounds/users
    log.info("📝 Updating /etc/hosts with known satellites, grounds and users...")
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
    local_ip
    ):

    # ⛔ If already exists, do nothing
    if link_exists(vxlan_if):
        log.info(f"♻️ Link {vxlan_if} already exists, updating...")
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
    run(["ip", "link", "set", "dev", vxlan_if, "up"])
    
    ## Assign IP from my_config subnet, if available. Last IP in subnet assigned to all vxlan interfaces
    available_ips = list(ipaddress.ip_network(my_config.get("subnet_ip","")).hosts())
    if len(available_ips) > 0:
        ip_addr = str(available_ips[-1]) + "/32"
        run(["ip", "addr", "add", ip_addr, "dev", vxlan_if])
        log.info(f"  ✅ VXLAN {vxlan_if} created with IP {ip_addr}.")
        if l3_flags.get("enable-routing", False):
            msg, success = routing.link_add(cli, my_node_name, vxlan_if)
            if success:
                log.info(msg)
            else:
                log.error(msg)

def delete_vxlan_link(
    vxlan_if):
    log.info(f"✂️ Deleting Link: {vxlan_if}")

    run([
        "ip", "link", "del", vxlan_if
    ])
    log.info(f"  ✅ VXLAN {vxlan_if} deleted.")
    if l3_flags.get("enable-routing", False):
        msg, success = routing.link_del(cli, my_node_name, vxlan_if)
        if success:
            log.info(msg)
        else:
            log.error(msg)

def apply_tc_settings(vxlan_if, netem_opts):
    """
    Apply tc netem settings to an interface.

    Parameters
    ----------
    vxlan_if : str
        Interface name (e.g., vxlan100)
    netem_opts : dict
        Dictionary of netem options, e.g.:
        {
            "delay": "50ms",
            "loss": "1%",
            "rate": "10mbit",
            "duplicate": "0.1%",
            "reorder": "25% 50%",
            "corrupt": "0.01%"
        }
    """
    log.info(f"  🎛️ Applying TC netem on {vxlan_if}: {netem_opts}")

    # Remove existing qdisc (ignore errors)
    run(["tc", "qdisc", "del", "dev", vxlan_if, "root"], log_errors=False)

    # Add new netem qdisc with specified options
    cmd = ["tc", "qdisc", "add", "dev", vxlan_if, "root", "netem"]
    for key, value in netem_opts.items():
        if value is None:
            continue
        # Allow both scalar and multi-argument options
        if isinstance(value, (list, tuple)):
            cmd.append(key)
            cmd.extend(str(v) for v in value)
        else:
            cmd.extend([key, str(value)])

    run(cmd)

def process_link_action(cli, event):
    try:
        tc_flag = l3_flags.get("enable-netem", True)
        key_str = event.key.decode()
        # Extract interface name, it is the last part of the key after /
        vxlan_if = key_str.split('/')[-1]

        ## Process PutEvent (Add/Update)
        if isinstance(event, etcd3.events.PutEvent):
                l = json.loads(event.value.decode())
                ep1, ep2 = l.get("endpoint1"), l.get("endpoint2")
                if ep1 != my_node_name and ep2 != my_node_name:
                    log.error(f"❌ Link action {ep1}<->{ep2} not relevant to this node.")
                    return

                ip1 = get_remote_ip(cli, ep1)
                ip2 = get_remote_ip(cli, ep2)
                if not ip1 or not ip2:
                    log.error(f"❌ Missing IPs for link action {ep1}<->{ep2}.")
                    return
                
                vni = str(l.get("vni", "0"))
                if ep1 == my_node_name:
                    remote_ip = ip2
                    local_ip = ip1
                else:
                    remote_ip = ip1
                    local_ip = ip2
                create_vxlan_link(
                    vxlan_if=vxlan_if,
                    target_vni=vni,
                    remote_ip=remote_ip,
                    local_ip=local_ip,
                )
                if tc_flag:
                    netem_opts=build_netem_opts(l)
                    if netem_opts:
                        apply_tc_settings(
                            vxlan_if=vxlan_if,
                            netem_opts=netem_opts
                    ) 
                    else:
                        log.info(f"  🎛️  No netem options defined for {vxlan_if}, skipping tc")
        
        ## Process DeleteEvent
        elif isinstance(event, etcd3.events.DeleteEvent):
                # interface delete removes possible TC automatically 
                delete_vxlan_link(vxlan_if)
    except: 
        log.error("  ❌ Failed to parse link action.")
        return

# ----------------------------
#   WATCHERS
# ----------------------------
def watch_link_actions_loop():
    log.info("👀 Watching /config/links (Dynamic Events)...")
    events_iterator, cancel = cli.watch_prefix(KEY_LINKS)
    for event in events_iterator:
        process_link_action(cli, event)

def watch_command_loop():
    log.info("👀 Watching Runtime Commands...")
    while True:
        try:
            events, _ = cli.watch(KEY_RUN)
            for e in events:
                if e.value: execute_commands(e.value.decode())
        except: time.sleep(5)

def watch_etchosts_loop():
    log.info("👀 Watching /config/etchosts (Dynamic Events)...")
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

        sat_found = update_key(f"/config/satellites/{my_node_name}")
        user_found = update_key(f"/config/users/{my_node_name}")
        ground_found = update_key(f"/config/grounds/{my_node_name}")
        return (sat_found or user_found or ground_found)
    except: return False

def get_config(cli):
    val, _ = cli.get(f"/config/satellites/{my_node_name}")
    if not val: val, _ = cli.get(f"/config/users/{my_node_name}")
    if not val: val, _ = cli.get(f"/config/grounds/{my_node_name}")
    if not val: return
    return json.loads(val.decode())

def main():
    global my_config, l3_flags, cli, routing
    log.info(f"🚀 Sat Agent Starting for {my_node_name}")
    cli = get_etcd_client()
    l3_flags = get_l3_flags_values(cli)
    my_config = get_config(cli)
    
    # Bootstrapping
    ## Register my IP address in Etcd
    while True:
        if register_my_ip(cli): break
        time.sleep(2)

    # L3 Routing Init
    isis_flags = l3_flags.get("enable-routing", True)
    if isis_flags:
        routing_mod_name = l3_flags.get("routing-module", "extra.isis")
        routing = __import__(routing_mod_name, fromlist=[''])
        msg, success = routing.init(cli, my_node_name)
        if success:
            log.info(msg)
        else:
            log.error(msg)

    # Initial Links Setup
    process_initial_topology(cli)

    ## Publish node IP for etc hosts usage 
    available_ips = list(ipaddress.ip_network(my_config.get("subnet_ip","")).hosts())
    ips_mask = my_config.get("subnet_ip","").split('/')[1]
    if len(available_ips) > 0:
        cli.put(f"/config/etchosts/{my_node_name}", str(available_ips[-1]))

    # Start Event Loops
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