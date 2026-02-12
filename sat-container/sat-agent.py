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
ETCD_USER = os.getenv("ETCD_USER", None)
ETCD_PASSWORD = os.getenv("ETCD_PASSWORD", None)
ETCD_CA_CERT = os.getenv("ETCD_CA_CERT", None)

if ":" in ETCD_ENDPOINT:
    h, p = ETCD_ENDPOINT.split(":", 1)
    ETCD_HOST, ETCD_PORT = h, int(p)
else:
    ETCD_HOST, ETCD_PORT = ETCD_ENDPOINT, 2379

my_node_name = os.getenv("NODE_NAME")

# KEYS
KEY_LINKS_PREFIX = f"/config/links/{my_node_name}/"
KEY_RUN = f"/config/run/{my_node_name}"

logging.basicConfig(level="INFO", format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# GLOBAL STATE
LINK_STATE_CACHE = {} # Only used for the initial sync
l3_flags = None
my_config = None
etcd_client = None
routing = None
HOSTS_LOCK = threading.Lock()

# ----------------------------
#   Helpers
# ----------------------------
def get_etcd_client():
    log.info(f"📁 Connecting to Etcd at {ETCD_HOST}:{ETCD_PORT}...")
    while True:
        try:
            if ETCD_USER and ETCD_PASSWORD:
                etcd_client = etcd3.client(host=ETCD_HOST, port=ETCD_PORT, user=ETCD_USER, password=ETCD_PASSWORD, ca_cert=ETCD_CA_CERT)
            else:
                etcd_client = etcd3.client(host=ETCD_HOST, port=ETCD_PORT)
            etcd_client.status()  # Test connection, if fail will raise
            log.info(f" ✅ Connected to Etcd at {ETCD_HOST}:{ETCD_PORT}.")
            return etcd_client
        except:
            time.sleep(5)

def get_remote_ip(etcd_client, node_name):
    val, _ = etcd_client.get(f"/config/nodes/{node_name}")
    if val:
        try: return json.loads(val.decode()).get("eth0_ip")
        except: pass
    return None

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
        "limit",
        "delay",
        "rate",
        "loss",
    ]:
        val = l.get(key)
        if val not in (None, "", []):
            netem_opts[key] = val
    return netem_opts


def derive_sysid_from_string(value: str) -> str:
    """
    Derive an 8-digit IS-IS system-id from an arbitrary string
    using a cryptographic hash (deterministic, stable).
    """
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
    """
    Return a single 'stable' usable IP inside `net`, without iterating.
    Mirrors the existing behavior ("last IP") but works for IPv6 too.
    """
    if net is None:
        return None

    # Single-address networks (e.g., /32 IPv4 or /128 IPv6)
    if net.num_addresses == 1:
        return net.network_address

    # IPv4 has broadcast for prefixes <= /30. For /31 and /32 there is no broadcast.
    if net.version == 4 and net.prefixlen <= 30:
        # usable range is [network+1, broadcast-1]
        return net.broadcast_address - 1

    # IPv6: treat the "highest address" as usable (no broadcast concept)
    return net.network_address + (net.num_addresses - 1)


# ----------------------------
#   Initial Setup
# ----------------------------
def process_initial_topology(etcd_client):
    """
    Reads /config/links and builds the initial world state.
    Uses 'add' action for everything found.
    """
    log.info("🏗️  Processing Initial Topology ...")
    
    ## Process links add
    tc_flag = l3_flags.get("enable-netem", True)
    for value, meta in etcd_client.get_prefix(KEY_LINKS_PREFIX):
        l = json.loads(value.decode())
        ep1, ep2 = l.get("endpoint1"), l.get("endpoint2")
        
        if ep1 != my_node_name and ep2 != my_node_name: 
            log.info(f"⚠️  Skipping initial link {ep1}<->{ep2} not relevant to this node.")
            continue

        ## Get remote IPs in a retry loop of 10 attempts
        ip1 = ip2 = None
        counter = 0
        while counter < 10:
            ip1 = get_remote_ip(etcd_client, ep1)
            ip2 = get_remote_ip(etcd_client, ep2)
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
    val, _ = etcd_client.get(KEY_RUN)
    if val:
        execute_commands(val.decode())
    
    log.info("📝 Updating /etc/hosts with known nodes (IPv4 + IPv6, one line per hostname)...")

    ## enabling ipv6 and ipv4 forwarding
    run(["sysctl", "-w", "net.ipv4.ip_forward=1"])
    run(["sysctl", "-w", "net.ipv6.conf.all.forwarding=1"])

    def _bootstrap_hosts_from_prefix(prefix: str):
        for value, meta in etcd_client.get_prefix(prefix):
            node_name = meta.key.decode().split('/')[-1]
            ip_addr = value.decode().strip()
            if not ip_addr:
                continue
            update_hosts_entry(node_name, ip_addr)

    # Prefer IPv4 first, then IPv6 overwrites (or vice versa if you swap order).
    _bootstrap_hosts_from_prefix("/config/etchosts6/")
    _bootstrap_hosts_from_prefix("/config/etchosts/")
    

# ----------------------------
#   Link Management & Runtime Commands
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
    
    log.info(f"🛜 Creating Link: {vxlan_if} (VNI: {target_vni})")

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
    
    # ----------------------------
    # L3 addressing (IPv4 / IPv6)
    # ----------------------------
    l3_cfg = my_config.get("L3-config", {})

    # IPv4 (existing behavior, but without hosts() iteration)
    v4_net = _parse_cidr(l3_cfg.get("cidr", ""))
    v4_ip = pick_last_usable_ip(v4_net)
    if v4_ip:
        v4_addr = f"{v4_ip}/32"
        run(["ip", "addr", "add", v4_addr, "dev", vxlan_if])
        log.info(f" ✅ VXLAN {vxlan_if} IPv4 set to {v4_addr}.")

    # IPv6
    v6_net = _parse_cidr(l3_cfg.get("cidr-v6", ""))
    v6_ip = pick_last_usable_ip(v6_net)
    if v6_ip:
        # Mirror the IPv4 choice (/32) with a host-route style /128
        v6_addr = f"{v6_ip}/128"
        run(["ip", "-6", "addr", "add", v6_addr, "dev", vxlan_if])
        log.info(f" ✅ VXLAN {vxlan_if} IPv6 set to {v6_addr}.")

    # Routing hook (keep your current behavior)
    if (v4_ip or v6_ip) and l3_flags.get("enable-routing", False) and routing is not None:
        msg, success = routing.link_add(etcd_client, my_node_name, vxlan_if)
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
    log.info(f" ✅ VXLAN {vxlan_if} deleted.")
    if l3_flags.get("enable-routing", False):
        msg, success = routing.link_del(etcd_client, my_node_name, vxlan_if)
        if success:
            log.info(msg)
        else:
            log.error(msg)

def apply_tc_settings(vxlan_if, netem_opts):
    """
    Apply tc netem settings to an interface, using:
      - change if a netem root qdisc already exists
      - add if no root qdisc exists
      - replace (or del+add fallback) if a non-netem root exists
    """
    if not netem_opts:
        log.info(f" 🎛️ No netem options provided for {vxlan_if}, skipping tc.")
        return
    
    log.info(f"  🎛️ Applying TC netem on {vxlan_if}: {netem_opts}")

    # Build netem command args (shared by add/change/replace)
    netem_args = ["netem"]
    if "rate" in netem_opts:
        netem_args += ["rate", str(netem_opts["rate"])]
    if "delay" in netem_opts:
        netem_args += ["delay", str(netem_opts["delay"])]
    if "loss" in netem_opts:
        netem_args += ["loss", "random", str(netem_opts["loss"])]
    if "limit" in netem_opts:
        netem_args += ["limit", str(netem_opts["limit"])]

    # --- Detect current root qdisc ---
    # Expect your run() to return stdout as a string when capture_output=True
    out = run(["tc", "qdisc", "show", "dev", vxlan_if], log_errors=False) or ""
    
    if "netem" in out.stdout:
        # Fast path: existing netem root -> change options in place
        cmd = ["tc", "qdisc", "change", "dev", vxlan_if, "root"] + netem_args
        run(cmd)
        return
    else: 
        cmd = ["tc", "qdisc", "add", "dev", vxlan_if, "root"] + netem_args
        run(cmd)
        return

def process_link_action(etcd_client, event):
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
                    log.error(f" ❌ Link action {ep1}<->{ep2} not relevant to this node.")
                    return

                ip1 = get_remote_ip(etcd_client, ep1)
                ip2 = get_remote_ip(etcd_client, ep2)
                if not ip1 or not ip2:
                    log.error(f" ❌ Missing IPs for link action {ep1}<->{ep2}.")
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
                        log.info(f" 🎛️  No netem options defined for {vxlan_if}, skipping tc")
        
        ## Process DeleteEvent
        elif isinstance(event, etcd3.events.DeleteEvent):
                # interface delete removes possible TC automatically 
                delete_vxlan_link(vxlan_if)
    except: 
        log.error(" ❌ Failed to parse link action.")
        return

# ----------------------------
#   WATCHERS
# ----------------------------
def watch_link_actions_loop():
    log.info("👀 Watching /config/links (Dynamic Events)...")
    backoff = 1
    while True:
        cancel = None
        try:
            events_iterator, cancel = etcd_client.watch_prefix(KEY_LINKS_PREFIX)
            for event in events_iterator:
                process_link_action(etcd_client, event)
        except Exception as ex:
            log.exception("❌ Failed to watch link actions (will retry).")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
        finally:
            if cancel is not None:
                try:
                    cancel()
                except Exception:
                    pass

def watch_command_loop():
    log.info("👀 Watching Runtime Commands...")
    backoff = 1
    while True:
        cancel = None
        try:
            events, cancel = etcd_client.watch(KEY_RUN)
            for e in events:
                if not getattr(e, "value", None):
                    continue
                try:
                    execute_commands(e.value.decode())
                except Exception:
                    # This ensures a bad value never kills the watch loop
                    log.exception("❌ Runtime command processing failed (but watcher continues).")           
        except Exception as ex:
            log.exception("❌ Failed to watch runtime commands (will retry).")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
        finally:
            if cancel is not None:
                try:
                    cancel()
                except Exception:
                    pass
   

def update_hosts_entry(node_name: str, ip_addr: str) -> None:
    """
    Ensure /etc/hosts contains exactly one entry for node_name:
        <ip_addr>\t<node_name>
    Removes any previous entries for node_name (IPv4 or IPv6).
    """
    if not node_name or not ip_addr:
        return

    try:
        with HOSTS_LOCK:
            # Read current hosts
            with open("/etc/hosts", "r") as f:
                hosts_content = f.read()

            # Remove any existing entry for this hostname (any IP)
            # Matches lines like: "<anything>  node_name" with spaces/tabs
            hosts_content = re.sub(
                rf"^[^\n]*\s+{re.escape(node_name)}\s*$\n?",
                "",
                hosts_content,
                flags=re.MULTILINE
            )

            # Append the new entry
            if not hosts_content.endswith("\n"):
                hosts_content += "\n"
            hosts_content += f"{ip_addr}\t{node_name}\n"

            with open("/etc/hosts", "w") as f:
                f.write(hosts_content)

        log.info(f"✅ Updated /etc/hosts entry: {ip_addr} {node_name}")
    except Exception as e:
        log.error(f"❌ Failed to update /etc/hosts for {node_name}: {e}")

def remove_hosts_entry(node_name: str) -> None:
    """
    Remove any /etc/hosts entry for node_name (IPv4 or IPv6).
    """
    if not node_name:
        return
    try:
        with HOSTS_LOCK:
            with open("/etc/hosts", "r") as f:
                hosts_content = f.read()

            new_content = re.sub(
                rf"^[^\n]*\s+{re.escape(node_name)}\s*$\n?",
                "",
                hosts_content,
                flags=re.MULTILINE
            )

            with open("/etc/hosts", "w") as f:
                f.write(new_content)

        log.info(f"✅ Removed /etc/hosts entry for: {node_name}")
    except Exception as e:
        log.error(f"❌ Failed to remove /etc/hosts entry for {node_name}: {e}")

def watch_etchosts_prefix(prefix: str):
    """
    Watch a given etc-hosts prefix (IPv4 or IPv6) and apply changes to /etc/hosts.
    Values are expected to be literal IP strings (v4 or v6).
    """
    log.info(f"👀 Watching {prefix} (Dynamic Events)...")
    backoff = 1
    while True:
        cancel = None
        try:
            events_iterator, cancel = etcd_client.watch_prefix(prefix)
            for event in events_iterator:
                try:
                    node_name = event.key.decode().split('/')[-1]

                    if isinstance(event, etcd3.events.PutEvent):
                        ip_addr = event.value.decode().strip()
                        if ip_addr:
                            update_hosts_entry(node_name, ip_addr)

                    elif isinstance(event, etcd3.events.DeleteEvent):
                        remove_hosts_entry(node_name)

                except Exception:
                    log.exception(f"❌ Failed to process {prefix} event.")
                    continue

        except Exception:
            log.exception(f"❌ Failed to watch {prefix} (will retry).")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
        finally:
            if cancel is not None:
                try:
                    cancel()
                except Exception:
                    pass       

def run_commands_sequentially(commands):
    for cmd in commands:
        log.info("▶️  exec: %s", cmd)
        subprocess.run(
            ["/bin/bash", "-c", cmd],
            shell=False,
            check=False
        )

def execute_commands(commands_raw_str: str) -> None:
    if not commands_raw_str:
        return
    try:
        commands = json.loads(commands_raw_str)
        if not isinstance(commands, list) or not all(isinstance(c, str) for c in commands):
            return
        threading.Thread(
            target=run_commands_sequentially,
            args=(commands,),
            daemon=True
        ).start()
    except Exception:
        log.exception("Failed to parse commands JSON")

# ----------------------------
#   INIT & MAIN
# ----------------------------
def register_my_ip(etcd_client):
    try:
        cmd = "ip -4 addr show eth0 | grep -oP '(?<=inet\s)\d+(\.\d+){3}' | head -n1"
        my_ip = subprocess.check_output(cmd, shell=True).decode().strip()
        if not my_ip or my_ip.endswith(".0"): return False
        
        def update_key(key):
            val, _ = etcd_client.get(key)
            if not val: return False
            try:
                data = json.loads(val.decode())
                if data.get("eth0_ip") != my_ip:
                    data["eth0_ip"] = my_ip
                    etcd_client.put(key, json.dumps(data))
                return True
            except: return False

        node_found = update_key(f"/config/nodes/{my_node_name}")
        return node_found
    except: return False

def get_config(etcd_client):
    val, _ = etcd_client.get(f"/config/nodes/{my_node_name}")
    if not val: return
    return json.loads(val.decode())

def main():
    global my_config, l3_flags, etcd_client, routing
    log.info(f"🚀 Sat Agent Starting for {my_node_name}")
    etcd_client = get_etcd_client()
    my_config = get_config(etcd_client)
    l3_flags = my_config.get("L3-config", {})
    
    # Bootstrapping


    # L3 Routing Init
    routing_flags = l3_flags.get("enable-routing", False)
    if routing_flags:
        routing_mod_name = l3_flags.get("routing-module", "extra.isis")
        routing = __import__(routing_mod_name, fromlist=[''])
        try:
            log.info(f"🌐 Initializing L3 Routing using module: {routing_mod_name} ...")
            msg, success = routing.init(etcd_client, my_node_name)
            if success:
                log.info(msg)
            else:
                log.error(msg)
        except Exception as e:
            log.error(f"❌ Failed to initialize L3 routing: {e}")
            routing = None
    
    # Publish node IPs for /etc/hosts usage
    l3_cfg = my_config.get("L3-config", {})

    v4_net = _parse_cidr(l3_cfg.get("cidr", ""))
    v4_ip = pick_last_usable_ip(v4_net)
    if v4_ip:
        etcd_client.put(f"/config/etchosts/{my_node_name}", str(v4_ip))

    v6_net = _parse_cidr(l3_cfg.get("cidr-v6", ""))
    v6_ip = pick_last_usable_ip(v6_net)
    if v6_ip:
        etcd_client.put(f"/config/etchosts6/{my_node_name}", str(v6_ip))
    
    ## Insert sat-vnet-super-cidr route with default gateway as next hop. 
    ## Necessary for vxlan setup in case of default gateway overriding to mimic satellite gateway behavior. 
    worker_name = my_config.get("worker", {})
    val, _ = etcd_client.get(f"/config/workers/{worker_name}")
    if not val:
        log.error(f"❌ Failed to fetch config data for worker {worker_name}. No config found.")
        sys.exit(1)
    worker_cfg = json.loads(val.decode())
    if "sat-vnet-super-cidr" not in worker_cfg:
        log.error(f"❌ Failed to fetch sat-vnet-super-cidr for worker {worker_name}. Key 'sat-vnet-super-cidr' not found.")
        sys.exit(1)
    sat_vnet_super_cidr = worker_cfg["sat-vnet-super-cidr"]
    cmd = ["ip", "route", "show", "default"]
    result = run(cmd)
    if result.returncode != 0:
        log.error("❌ Failed to fetch default gateway for sat-vnet-super-cidr route.")
        sys.exit(1)
    default_gw = result.stdout.strip().split()[2]
    cmd = ["ip", "route", "add", sat_vnet_super_cidr, "via", default_gw]
    run(cmd)

    # Start Event Loops
    threads = [
        threading.Thread(target=watch_link_actions_loop, daemon=True), # Dynamic
        threading.Thread(target=watch_command_loop, daemon=True),
        threading.Thread(target=watch_etchosts_prefix, args=("/config/etchosts/",), daemon=True),
        threading.Thread(target=watch_etchosts_prefix, args=("/config/etchosts6/",), daemon=True)
    ]
    for t in threads: t.start()
    
    log.info(f"✅ All Watchers Started.")

    # Initial Links Setup
    process_initial_topology(etcd_client)

    ## Register my IP address in Etcd
    while True:
        if register_my_ip(etcd_client): break
        time.sleep(2)

    while True: time.sleep(1)

if __name__ == "__main__":
    try: main()
    except: sys.exit(0)