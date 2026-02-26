#!/usr/bin/env python3
import argparse
import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Tuple
import etcd3
import registration_request
import handover_request


# ----------------------------
# GLOBALS
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

grd_list = []
DEV_RE = re.compile(r"\bdev\s+([^\s]+)\b")
node_name = os.getenv("NODE_NAME")
KEY_LINKS_PREFIX = f"/config/links/{node_name}/"
link_setup_delay_s = 0.5 # estimated time needed by sat-agent to setup relevat routes and interfaces after a link is added in etcd, used to delay registration after link event to increase chances that the link is fully setup in the sat-agent before registration attempt (which can reduce registration failures due to missing routes/interfaces in the sat-agent at the time of registration)
registration_accept_timeout_s = 1.0
handover_command_timeout_s = 1.0

# Status not_registered, registration_in_progress, registered, handover_in_progress
status = "not_registered" # initial status before registration
current_link = None # current link info (dict with keys: endpoint1, endpoint2, delay, vni, etc.) used for registration and handover decisions
current_iface = None # current iface used for registration and handover (derived from current_link)
new_link = None # new link info used for handover decisions 

# ho eligibility strategy function, set in main() based on args
is_handover_eligible = None
grd_ipv6_runtime = None
grd_port_runtime = None
grd_id_runtime = None
callback_port_runtime = None
local_ipv6 = None
etcd_client_runtime = None
registration_timeout_timer = None
handover_timeout_timer = None

# ----------------------------
#   HELPERS
# ----------------------------
def get_etcd_client() -> etcd3.Etcd3Client:
    logging.info(f"📁 Connecting to Etcd at {ETCD_HOST}:{ETCD_PORT}...")
    while True:
        try:
            if ETCD_USER and ETCD_PASSWORD:
                etcd_client = etcd3.client(host=ETCD_HOST, port=ETCD_PORT, user=ETCD_USER, password=ETCD_PASSWORD, ca_cert=ETCD_CA_CERT)
            else:
                etcd_client = etcd3.client(host=ETCD_HOST, port=ETCD_PORT)
            etcd_client.status()  # Test connection, if fail will raise
            logging.info(f" ✅ Connected to Etcd at {ETCD_HOST}:{ETCD_PORT}.")
            return etcd_client
        except Exception as e:
            logging.warning(f" ❌ Failed to connect to Etcd at {ETCD_HOST}:{ETCD_PORT}: {e}, retry in 5 seconds...")
            time.sleep(5)

def sleep_until(deadline_ns: int, spin_ns: int = 200_000):  # spin last 0.2ms by default
    """Sleep until a monotonic deadline (ns)."""
    while True:
        now = time.monotonic_ns()
        remaining = deadline_ns - now
        if remaining <= 0:
            return
        # Sleep most of the remaining time
        if remaining > spin_ns:
            time.sleep((remaining - spin_ns) / 1e9)
        else:
            # Busy-wait for the final slice (more precise)
            pass

def run_cmd_capture(cmd: List[str]) -> str:
    res = subprocess.run(cmd, text=True, capture_output=True)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or f"Command failed: {' '.join(cmd)}")
    return res.stdout.strip()


def run_cmd(cmd: List[str]) -> None:
    logging.debug("EXEC: %s", " ".join(cmd))
    res = subprocess.run(cmd, text=True, capture_output=True)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or f"Command failed: {' '.join(cmd)}")


def derive_egress_dev(addr: str) -> str:
    out = run_cmd_capture(["ip", "-6", "route", "get", addr])
    m = DEV_RE.search(out)
    if not m:
        raise RuntimeError(f"Could not parse egress dev from: {out}")
    return m.group(1)


def build_srv6_route_replace(dst_prefix: str, sid: str, dev: str) -> List[str]:
    return [
        "ip", "-6", "route", "replace", dst_prefix,
        "encap", "seg6", "mode", "encap", "segs", sid,
        "dev", dev,
    ]

def init_qdisc() -> None:
    dev = "veth0_rt" # Assuming this is the shaping interfcace
    try:
        run_cmd(["tc", "qdisc", "del", "dev", dev, "root"])
    except:
        pass
    run_cmd(["tc", "qdisc", "add", "dev", dev, "root", "handle", "1:", "htb", "default", "20"])
    run_cmd(["tc", "class", "add", "dev", dev, "parent", "1:", "classid", "1:1", "htb", "rate", "10gbit", "ceil", "10gbit"])

def prepare_qdisc_for_grd(grd_ipv6: str, grd_id: str) -> None:
    dev = "veth0_rt" # Assuming this is the shaping interface
    dst = grd_ipv6.split("/")[0]  # Extract IP from prefix
    # derive user id as the position of username in the user_list 
    idx = grd_list.index(grd_id)
    run_cmd(["tc", "class", "add", "dev", dev, "parent", "1:", "classid", f"1:{idx+10}", "htb", "rate", "10gbit", "ceil", "10gbit"])
    run_cmd(["tc", "filter", "add", "dev", dev, "parent", "1:", "protocol", "ipv6", "prio", "10", "flower","dst_ip" ,dst, "action","pass","flowid" ,f"1:{idx+10}"])
    logging.info(f"🎛️ Applied created shaping qdisc and filter for {grd_id}, prefix {grd_ipv6}, on dev {dev}")       


def build_ipv6_default_route(grd_ipv6: str) -> List[str]:
    return [
        "ip", "-6", "route", "replace", "default",
        "via", grd_ipv6.split("/")[0],  # Extract IP from prefix
    ]

def cancel_registration_timeout() -> None:
    global registration_timeout_timer
    if registration_timeout_timer is not None:
        registration_timeout_timer.cancel()
        registration_timeout_timer = None

def on_registration_accept_timeout() -> None:
    global status, current_link, current_iface, etcd_client_runtime, registration_timeout_timer
    registration_timeout_timer = None
    if status != "registration_in_progress":
        return

    logging.warning("⏱️ Registration accept timeout reached. Resetting state and retrying registration.")
    status = "not_registered"
    current_link = None
    current_iface = None

    if etcd_client_runtime is not None:
        handle_registration_request(etcd_client=etcd_client_runtime)

def start_registration_timeout() -> None:
    global registration_timeout_timer
    cancel_registration_timeout()
    registration_timeout_timer = threading.Timer(registration_accept_timeout_s, on_registration_accept_timeout)
    registration_timeout_timer.daemon = True
    registration_timeout_timer.start()

def cancel_handover_command_timeout() -> None:
    global handover_timeout_timer
    if handover_timeout_timer is not None:
        handover_timeout_timer.cancel()
        handover_timeout_timer = None

def on_handover_command_timeout() -> None:
    global status, new_link
    if status != "handover_in_progress":
        return
    logging.warning("⏱️ Handover command timeout reached.")
    new_link = None
    status = "registered" if status == "handover_in_progress" else status
    cancel_handover_command_timeout()
    return

def start_handover_command_timeout(timeout_s: float) -> None:
    global handover_timeout_timer
    cancel_handover_command_timeout()
    handover_timeout_timer = threading.Timer(timeout_s, on_handover_command_timeout)
    handover_timeout_timer.daemon = True
    handover_timeout_timer.start()

def parse_delay(delay) -> float:
    if isinstance(delay, (int, float)):
        return float(delay)
    elif isinstance(delay, str):
        delay = delay.strip().lower()
        if delay.endswith("ms"):
            return float(delay[:-2])
        elif delay.endswith("s"):
            return float(delay[:-1]) * 1000
        elif delay.endswith("us"):
            return float(delay[:-2]) / 1000
        else:
            raise ValueError(f"Unknown delay format: {delay}")
    else:
        raise ValueError(f"Invalid delay type: {type(delay)}")
# ----------------------------
#   MAIN LOGIC
# ----------------------------


#  Registration
def handle_registration_request(etcd_client) -> None:
    """
    Reads /config/links and builds the initial world state.
    Uses 'add' action for everything found.
    """
    global status, current_link, grd_ipv6_runtime, grd_port_runtime, callback_port_runtime, local_ipv6, current_iface
    
    if status != "not_registered":
        logging.warning(f"⚠️  Skipping registration request since status is {status}")
        return
    logging.info("🌍  Processing Registration Request...")
    
    ## Process initial registration using link with minimum delay (if any) 
    min_delay_ms = float('inf')
    registration_sat = None
    registration_link_info = None
    for value, meta in etcd_client.get_prefix(KEY_LINKS_PREFIX):
        l = json.loads(value.decode())
        ep1, ep2 = l.get("endpoint1"), l.get("endpoint2")
        
        if ep1 != node_name and ep2 != node_name: 
            logging.warning(f"⚠️  Skipping link {ep1}<->{ep2} not relevant to this node.")
            continue

        delay_ms = parse_delay(l.get("delay", float('inf')))
        if registration_sat is None or delay_ms < min_delay_ms:
            min_delay_ms = delay_ms
            remote_endpoint = ep2 if ep1 == node_name else ep1
            registration_sat = remote_endpoint
            registration_link_info = l
    
    if registration_sat:
        init_sat_ipv6 = None
        try:
            init_sat_ipv6 = run_cmd_capture(["grep", registration_sat, "/etc/hosts"]).split()[0]
        except Exception as e:
            logging.error(f"❌ Failed to resolve access satellite IPv6 address {registration_sat}: {e}")
            return
        logging.info(f"🛰️ Found access link with {registration_sat}. Registering...")
        
        # add route to grd via initial satellite to ensure registration request can reach the grd
        dev = derive_egress_dev(init_sat_ipv6)
        ip_cmd = ["ip", "-6", "route", "replace", grd_ipv6_runtime, "via", init_sat_ipv6, "dev", dev]
        run_cmd(ip_cmd)
        
        # Here you would implement the actual registration logic, e.g. sending a registration request to the remote endpoint, etc.
        registration_request.send_registration_request(
            grd_ipv6=grd_ipv6_runtime, 
            grd_port=grd_port_runtime, 
            usr_ipv6=local_ipv6, 
            callback_port=callback_port_runtime, 
            init_sat_ipv6=init_sat_ipv6)
        
        current_link = registration_link_info
        current_iface = f"vl_{registration_sat}_1"
        status = "registration_in_progress"
        start_registration_timeout()
        # For this example, we just log the registration action.
        logging.info(f"✉️ Sent registration request via {registration_sat} to {grd_id_runtime}.")
    else:
        logging.warning("⚠️ No suitable access link found for registration.")

# ----------------------------
#   WATCHERS
# ----------------------------
def watch_link_actions_loop (etcd_client) -> None:
    global status, current_iface, current_link, new_link
    logging.info("👀 Watching /config/links (Dynamic Events)...")
    backoff = 1
    while True:
        cancel = None
        try:
            events_iterator, cancel = etcd_client.watch_prefix(KEY_LINKS_PREFIX)
            for event in events_iterator:
                if isinstance(event, etcd3.events.PutEvent):
                    if status == "not_registered":
                        time.sleep(link_setup_delay_s)
                        handle_link_action_for_registration(event)
                    elif status == "registered":
                        time.sleep(link_setup_delay_s)
                        handle_link_action_for_handover(event)
                elif isinstance(event, etcd3.events.DeleteEvent):
                    deleted_iface = event.key.decode().split("/")[-1]
                    if deleted_iface == current_iface:
                        logging.warning(
                            "🛑 Current interface %s deleted, resetting state and re-registering.",
                            deleted_iface,
                        )
                        status = "not_registered"
                        current_iface = None
                        current_link = None
                        new_link = None
                        time.sleep(link_setup_delay_s)
                        if handover_timeout_timer is not None:
                            handover_timeout_timer.cancel()
                        if registration_timeout_timer is not None:
                            registration_timeout_timer.cancel()
                        handle_registration_request(etcd_client=etcd_client)
        except Exception as ex:
            logging.exception("❌ Failed to watch link actions (will retry).")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
        finally:
            if cancel is not None:
                try:
                    cancel()
                except Exception:
                    pass

def is_handover_eligible_delay(current_link,new_link) -> bool:
    # Example eligibility check: only trigger handover if delay difference is > 20ms
    if current_link is None:
        return True
    new_link_delay_ms = parse_delay(new_link.get("delay", float('inf')))
    current_link_delay_ms = parse_delay(current_link.get("delay", float('inf')))
    delay_diff = new_link_delay_ms - current_link_delay_ms
    logging.debug(f"Evaluating handover eligibility: current delay {current_link.get('delay', 'N/A')} ms, new delay {new_link.get('delay', 'N/A')} ms, diff {delay_diff} ms")
    return delay_diff < -5  # Trigger handover if new link is at least 20ms better

def is_handover_eligible_newest(current_link,new_link) -> bool:
    return True  # Always eligible (for make-before-break)

def handle_link_action_for_registration(event) -> None:
    global status, etcd_client_runtime
    try:
        if status != "not_registered":
            logging.warning(f"⚠️ Ignoring link event for registration since status={status}")
            return
        if not isinstance(event, etcd3.events.PutEvent):
            logging.warning("⚠️ Ignoring non-PutEvent for registration.")
            return

        l = json.loads(event.value.decode())
        ep1, ep2 = l.get("endpoint1"), l.get("endpoint2")
        if ep1 != node_name and ep2 != node_name:
            return
        if etcd_client_runtime is None:
            logging.error("❌ Missing etcd client, cannot trigger registration on link event.")
            return

        logging.info(f"🛰️ New link {ep1}<->{ep2} while not_registered, triggering registration.")
        handle_registration_request(etcd_client=etcd_client_runtime)
    except Exception:
        logging.exception("❌ Failed to process link action for registration.")

def handle_link_action_for_handover(event):
    global current_link, status, new_link
    try:
        ## Process PutEvent (Add/Update)
        if not isinstance(event, etcd3.events.PutEvent):
            logging.warning("⚠️ Ignoring non-PutEvent for handover processing.")
            return
        l = json.loads(event.value.decode())
        ep1, ep2 = l.get("endpoint1"), l.get("endpoint2")
        if ep1 != node_name and ep2 != node_name:
            logging.error(f" ❌ Link action {ep1}<->{ep2} not relevant to this node.")
            return
        if status != "registered":
            logging.warning(f"⚠️ Ignoring link update while status={status}")
            return
        # Check if this is the current link (e.g., an update to the current link) or a new link
        if (current_link is not None and ((ep1 == current_link.get("endpoint1") and ep2 == current_link.get("endpoint2")) or (ep1 == current_link.get("endpoint2") and ep2 == current_link.get("endpoint1")))):
            logging.info(f"🔄 Detected update to current link {ep1}<->{ep2}, updating current link info.")
            current_link = l
            return
        # Evaluate handover decision
        if not is_handover_eligible(current_link, l):
            logging.info(f"ℹ️ New link {ep1}<->{ep2} not eligible for handover.")
            return

        remote_endpoint = ep2 if ep1 == node_name else ep1
        try:
            new_sat_ipv6 = run_cmd_capture(["grep", remote_endpoint, "/etc/hosts"]).split()[0]
        except Exception as e:
            logging.error(f"❌ Failed to resolve new satellite {remote_endpoint}: {e}")
            return

        handover_request.send_handover_request(
            grd_ipv6=grd_ipv6_runtime,
            port=grd_port_runtime,
            user_ipv6=local_ipv6,
            callback_port=callback_port_runtime,
            new_sat_ipv6=new_sat_ipv6,
        )
        status = "handover_in_progress"
        new_link = l
        logging.info(f"✉️ Sent handover request for new sat {remote_endpoint} to {grd_id_runtime}.")
        start_handover_command_timeout(timeout_s=handover_command_timeout_s)
    except Exception:
        logging.exception("❌ Failed to process handover")
        return

def handle_handover_command(payload: Dict[str, Any], ho_delay_ms: float) -> None:
    global status, current_link, new_link, current_iface

    if status != "handover_in_progress":
        logging.warning("⚠️ Received handover_command while not in handover_in_progress state, ignoring.")
        return

    grd_id = payload["grd_id"]      # e.g. "grd1"
    grd_ipv6 = payload["grd_ipv6"]  # e.g. "2001:db8:101::1/128"
    upstream_sids = payload["sids"]                  # new sid sequence for the user (e.g., "2001:db8:200::1")
    first_sid = upstream_sids.split(",")[0]          # first SID is the new egress SID to reach the grd. Shall be the IP address of a connected sat
    dev = derive_egress_dev(first_sid)

    # Apply handover delay rate reduction if configured (e.g., to allow user to switch satellite link)
    if ho_delay_ms > 0:
        logging.info("⧴ Applying handover delay of %dms", ho_delay_ms)
        mtu = 1500  # Assuming MTU for shaping rules
        rate_kbit = max(1, int(mtu * 8 / ho_delay_ms))  # kbit/s (since ms in denominator)
        burst_bytes = mtu * 2
        cburst_bytes = mtu * 2
        idx = grd_list.index(payload["grd_id"])

        run_cmd([
        "tc","class","change","dev","veth0_rt",
        "parent","1:","classid",f"1:{idx+10}",
        "htb",
        "rate",f"{rate_kbit}kbit","ceil",f"{rate_kbit}kbit",
        "burst",f"{burst_bytes}b","cburst",f"{cburst_bytes}b",
        ])

        deadline = time.monotonic_ns() + int(ho_delay_ms * 1_000_000)
        sleep_until(deadline)
        
        # Restore original qdisc after delay
        run_cmd([
        "tc","class","change","dev","veth0_rt",
        "parent","1:","classid",f"1:{idx+10}",
        "htb",
        "rate","10gbit","ceil","10gbit",
        "burst","15kb","cburst","15kb",   # example “normal” values
        ])
        logging.info("⧴ Handover delay completed, restored original qdisc settings")
    
    # add route to grd via initial satellite to ensure registration request can reach the grd
    ip_cmd = ["ip", "-6", "route", "replace", grd_ipv6_runtime, "via", first_sid, "dev", dev]
    run_cmd(ip_cmd)
    default_prefix = "default"
    ip_cmd = build_srv6_route_replace(default_prefix, upstream_sids, dev)
    run_cmd(ip_cmd)
    current_link = new_link # after handover we consider the current link info as unknown until next update from etcd
    current_iface = dev
    new_link = None
    status = "registered"

    logging.info(f"📡 Handover accepted by {grd_id} with via {upstream_sids} dev {dev}")

def handle_registration_accept(payload: Dict[str, Any]) -> None:
    global status

    if status != "registration_in_progress":
        logging.debug("⚠️ Received registration_accept while not in registration_in_progress state, ignoring.")
        return

    grd_ipv6 = payload["grd_ipv6"]  # e.g. "2001:db8:101::1/128"
    grd_id = payload["grd_id"]            # e.g. "grd1"
    upstream_sids = payload["sids"]                  # new sid sequence for the user (e.g., "2001:db8:200::1")
    first_sid = upstream_sids.split(",")[0]          # first SID is the new egress SID to reach the grd. Shall be the IP address of a connected sat
    dev = derive_egress_dev(first_sid)
    logging.debug("Derived egress dev for SID %s: %s", upstream_sids, dev)

    # add ipv6 default route via grd_ipv6
    default_prefix = "default"
    ip_cmd = build_srv6_route_replace(default_prefix, upstream_sids, dev)
    run_cmd(ip_cmd)
    cancel_registration_timeout()
    status = "registered"
    logging.info(f"📡 Registration accepted by {grd_id} with via {upstream_sids} dev {dev}")
    

def handle_command(payload: Dict[str, Any], ho_delay_ms: float, grd_id: int) -> None:
    if payload.get("type") == "handover_command":
        handle_handover_command(payload, ho_delay_ms)
        return
    elif payload.get("type") == "registration_accept":
        handle_registration_accept(payload)
    else:
        raise ValueError(f"Unsupported command type: {payload.get('type')}")

def serve(bind_addr: str, port: int, ho_delay: float) -> None:
    sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    sock.bind((bind_addr, port))
    logging.info("⚙️ usr_agent listening on [%s]:%d", bind_addr, port)

    # prepare qdisk for users (if ho_delay is set)
    if ho_delay > 0:
        init_qdisc()

    while True:
        data, peer = sock.recvfrom(4096)
        try:
            msg = json.loads(data.decode("utf-8"))
            grd_id = msg.get("grd_id", "unknown")
            if grd_id not in grd_list:
                # add new grd to grd list and prepare qdisc for them
                grd_list.append(grd_id)
                if ho_delay > 0:
                    prepare_qdisc_for_grd(grd_ipv6=msg.get("grd_ipv6"), grd_id=grd_id)
            logging.debug("RX from [%s]:%d msg=%s", peer[0], peer[1], msg)
            handle_command(msg, ho_delay_ms=ho_delay, grd_id=grd_id)
        except Exception as e:
            logging.warning("❌ Failed command from [%s]:%d: %s", peer[0], peer[1], e)


def main() -> None:
    global is_handover_eligible, grd_ipv6_runtime, grd_port_runtime, grd_id_runtime, callback_port_runtime, local_ipv6, etcd_client_runtime, registration_accept_timeout_s
    ap = argparse.ArgumentParser()
    ap.add_argument("--bind", default="::")
    ap.add_argument("--port", type=int, default=5006, help="UDP port where usr1 listens for handover_command")
    ap.add_argument("--ho-delay", type=float, help="Handover delay in mseconds (requires veth0_rt, use app/shaping-ns-create-v6.sh)", default=0)
    ap.add_argument("--grd", required=True, help="IPv6 address of the serving ground station or name resolvable via /etc/hosts (e.g., grd1 or 2001:db8:101::1)")
    ap.add_argument("--grd-port", type=int, default=5005, help="UDP port on serving ground station (default: 5005)")
    ap.add_argument("--registration-timeout", type=float, default=5.0, help="seconds to wait for registration_accept before retrying registration")
    ap.add_argument("--no-auto", action="store_true", help="disable automatic handover and registration")
    ap.add_argument("--ho-strategy", help='handover eligibility strategy: "delay" (default, triggers handover if delay improvement > 5ms) or "newest" (always trigger handover for newest link)', default="delay")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    
    local_ipv6 = run_cmd_capture(["grep", os.environ["NODE_NAME"], "/etc/hosts"]).split()[0]
    # resolve grd_address if it's a hostname
    grd_addr = args.grd
    if not ":" in args.grd:  # crude check for hostname vs IPv6
        try:
            grd_addr = run_cmd_capture(["grep", args.grd, "/etc/hosts"]).split()[0]
            logging.debug("Resolved ground station address %s to %s", args.grd, grd_addr)
        except Exception as e:
            logging.error(f"Failed to resolve ground station address {args.grd}: {e}")
            sys.exit(1)
    
    grd_id_runtime = args.grd
    grd_ipv6_runtime = grd_addr
    grd_port_runtime = args.grd_port
    callback_port_runtime = args.port
    registration_accept_timeout_s = args.registration_timeout

    etcd_client=get_etcd_client()
    etcd_client_runtime = etcd_client
    
    if args.no_auto:
        logging.info("🚫 Auto handover and registration disabled, skipping initial registration.")
    else:
        handle_registration_request(etcd_client=etcd_client)
    
    if args.ho_strategy == "delay":
        is_handover_eligible = is_handover_eligible_delay
    elif args.ho_strategy == "newest":
        is_handover_eligible = is_handover_eligible_newest
    else:
        logging.error(f"Unsupported handover strategy: {args.ho_strategy}")
        sys.exit(1)

    if not args.no_auto:
        threading.Thread(
            target=watch_link_actions_loop,
            args=(etcd_client,),
            daemon=True,
            name="watch-link-actions",
        ).start()
    
    serve(bind_addr=args.bind, port=args.port, ho_delay=args.ho_delay)

if __name__ == "__main__":
    main()
