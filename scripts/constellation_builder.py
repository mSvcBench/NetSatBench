#!/usr/bin/env python3
import etcd3
import subprocess
import json
import os
import sys

# ==========================================
# 🚩 CONFIGURATION FLAGS
# ==========================================
# Set to False to skip IS-IS routing configuration
ENABLE_ISIS = True

# Set to False to ignore Bandwidth/Latency rules even if they exist in Etcd
ENABLE_TC = False
# ==========================================

LOCAL_HOST = "host-1"
# Ensure we can connect to the Etcd server defined in your environment
try:
    etcd = etcd3.client(host='10.0.1.215', port=2379)
except Exception as e:
    print(f"❌ Failed to initialize Etcd client: {e}")
    sys.exit(1)

def get_prefix_data(prefix):
    """
    Fetches all keys under a prefix from Etcd and returns a dict.
    e.g., /config/satellites/sat3 -> data['sat3']
    """
    data = {}
    for value, metadata in etcd.get_prefix(prefix):
        key = metadata.key.decode('utf-8').split('/')[-1]
        try:
            data[key] = json.loads(value.decode('utf-8'))
        except json.JSONDecodeError:
            print(f"⚠️ Warning: Could not parse JSON for key {key}")
    return data

def get_links_from_connection_key():
    """
    Fetches the specific /config/links/connection key.
    """
    value, _ = etcd.get('/config/links/connection')
    if value:
        try:
            return json.loads(value.decode('utf-8'))
        except json.JSONDecodeError:
            print(f"❌ Error: /config/links/connection contains invalid JSON")
            return []
    return []

def get_ip_from_cidr(cidr, host_part):
    """
    Parses a CIDR like '192.168.3.0/32' or '192.168.3.0/24'
    and returns '192.168.3.{host_part}'
    """
    try:
        # Splits '192.168.3.0/32' -> ['192', '168', '3', '0/32']
        # Joins first 3 parts -> '192.168.3'
        base_ip = ".".join(cidr.split('.')[:3])
        return f"{base_ip}.{host_part}"
    except Exception as e:
        print(f"❌ Error parsing CIDR {cidr}: {e}")
        return None

# ==========================================
# 1. LOAD CONFIGURATION FROM ETCD
# ==========================================
try:
    satellites = get_prefix_data('/config/satellites/')
    links = get_links_from_connection_key()
    hosts = get_prefix_data('/config/hosts/')
except Exception as e:
    print(f"❌ Failed to read configuration from ETCD: {e}")
    sys.exit(1)

print(f" Found {len(satellites)} satellites")
print(f" Found {len(links)} links")
print(f" 🚩 Flags: ISIS={ENABLE_ISIS}, TC={ENABLE_TC}")

if not satellites or not links:
    print("⚠️  Warning: No satellites or links found. Check Etcd population.")

# ==========================================
# 2. CREATE SATELLITE CONTAINERS
# ==========================================
for name, sat in satellites.items():
    print(f"➞️ Creating satellite: {name}")
    
    # Validation: Host existence
    if sat['host'] not in hosts:
        print(f"❌ Error: Satellite {name} assigned to unknown host {sat['host']}")
        continue
        
    host_info = hosts[sat['host']]
    ssh_user = host_info['ssh_user']
    
    # Handle the space in " N_Antennas" from your JSON structure
    n_antennas = str(sat.get(' N_Antennas', sat.get('N_Antennas', 5)))

    cmd = ['./create-sat.sh', name, n_antennas, sat['host'], ssh_user]
    subprocess.run(cmd, check=True)

# ==========================================
# 3. CREATE VXLAN LINKS (PLUMBING & POLICY)
# ==========================================
sat_id_to_name = {sat.get("ID"): name for name, sat in satellites.items()}
used_antennas = {name: set() for name in satellites}

for link in links:
    # Resolve names using explicit fields or ID mapping
    src_name = link.get('src_sat') or sat_id_to_name.get(link.get('src-sat'))
    dst_name = link.get('dst_sat') or sat_id_to_name.get(link.get('dst-sat'))

    if not src_name or not dst_name:
        print(f"⚠️ Skipping link: unknown satellite names")
        continue

    src_antenna = link.get('src_antenna')
    dst_antenna = link.get('dst_antenna')

    if src_antenna is None or dst_antenna is None:
        print(f"⚠️ Skipping link {src_name}->{dst_name}: Missing antenna numbers")
        continue

    # Track usage to warn on collisions
    used_antennas.setdefault(src_name, set()).add(src_antenna)
    used_antennas.setdefault(dst_name, set()).add(dst_antenna)

    src_host = satellites[src_name]['host']
    dst_host = satellites[dst_name]['host']
    ssh_user = hosts[src_host]['ssh_user']
    
    # Interface Names (Standardized)
    src_iface = f"{dst_name}_a{dst_antenna}"
    dst_iface = f"{src_name}_a{src_antenna}"

    print(f"🔗 Creating VXLAN: {src_name}.a{src_antenna} <-> {dst_name}.a{dst_antenna}")

    # A. CALL ADD-LINK (Pure Plumbing - Layer 2)
    # This script must NOT accept TC arguments anymore
    link_cmd = [
        './add-link.sh', src_name, str(src_antenna),
        src_host, dst_name, str(dst_antenna),
        dst_host, ssh_user
    ]
    subprocess.run(link_cmd, check=True)

    # B. CALL APPLY-TC (Optional Policy - Layer 2 Shaping)
    if ENABLE_TC:
        # Check if the specific link has BW requirements in Etcd
        if 'bw' in link and 'burst' in link and 'latency' in link:
            tc_bw = link['bw']
            tc_burst = link['burst']
            tc_latency = link['latency']
            print(f"   ⚙️ Applying TC: {tc_bw}, {tc_latency}")

            # Apply to Source Interface
            subprocess.run(['./apply-tc.sh', src_name, src_iface, src_host, ssh_user, tc_bw, tc_burst, tc_latency], check=True)
            # Apply to Destination Interface
            subprocess.run(['./apply-tc.sh', dst_name, dst_iface, dst_host, ssh_user, tc_bw, tc_burst, tc_latency], check=True)
        else:
            print(f"   ℹ️ No TC measures in Etcd for this link. running unlimited.")

    # Enrich link object for IP calculation steps
    link['src_sat'] = src_name
    link['dst_sat'] = dst_name
    link['src_antenna'] = src_antenna
    link['dst_antenna'] = dst_antenna

# ==========================================
# 4. ASSIGN IP ADDRESSES (Inside Containers)
# ==========================================
for name, sat in satellites.items():
    # Prefer explicit 'sat_net_cidr' from JSON, fallback to ID-based generation
    if 'sat_net_cidr' not in sat:
        sat_id = int(sat.get('ID', 0))
        sat['sat_net_cidr'] = f"192.168.{sat_id}.0/24"
        print(f"  Auto-assigned sat_net_cidr: {sat['sat_net_cidr']}")
    
    # JSON provided uses " N_Antennas"
    n_antennas = str(sat.get(' N_Antennas', sat.get('N_Antennas', 5)))
    host_info = hosts[sat['host']]
    ssh_user = host_info['ssh_user']

    # Calls add-sat-addresses.sh to configure br1, br2, etc.
    ip_cmd = ['./add-sat-addresses.sh', name, n_antennas,
              sat['sat_net_cidr'], sat['host'], ssh_user]
    subprocess.run(ip_cmd, check=True)

# ==========================================
# 5. CALCULATE LINK IPS (For Verification/Logging)
# ==========================================
for link in links:
    src_name = link['src_sat']
    dst_name = link['dst_sat']
    
    # Get CIDR from satellite object (populated in Step 4)
    src_cidr = satellites[src_name]['sat_net_cidr']
    dst_cidr = satellites[dst_name]['sat_net_cidr']

    # Calculate exact IP for logging
    link['src_ant_ip'] = get_ip_from_cidr(src_cidr, link['src_antenna'])
    link['dst_ant_ip'] = get_ip_from_cidr(dst_cidr, link['dst_antenna'])

# ==========================================
# 6. CONFIGURE IS-IS (OPTIONAL ROUTING)
# ==========================================
if ENABLE_ISIS:
    print("\n📡 Configuring IS-IS Routing...")
    isis_interfaces = {}
    
    # Identify all satellites involved in links
    for link in links:
        isis_interfaces[link['src_sat']] = True
        isis_interfaces[link['dst_sat']] = True

    fixed_antennas = ['1', '2', '3', '4', '5']

    for sat_name in isis_interfaces.keys():
        sat = satellites.get(sat_name, {})
        
        # Generate NET ID (Area ID) if missing
        sat['net_id'] = sat.get('net_id', f"{int(sat.get('ID', 0)):04d}")
        
        # ---------------------------------------------------------
        # ⚡ CRITICAL FIX: FORCE NETWORK ADDRESS (Ends in .0) ⚡
        # ---------------------------------------------------------
        # 1. Get the raw value from Etcd 
        raw_cidr = sat.get('sat_net_cidr', f"192.168.{sat.get('ID', 0)}.0/24")

        # 2. Strip the mask (remove /24 or /32)
        ip_only = raw_cidr.split('/')[0]

        octets = ip_only.split('.')
        if len(octets) == 4:
            sat_cidr = f"{octets[0]}.{octets[1]}.{octets[2]}.0/24"
        else:
            # Fallback for weird data
            sat_cidr = raw_cidr

        # ---------------------------------------------------------

        host_info = hosts[sat['host']]
        ssh_user = host_info['ssh_user']

        print(f"   Configuring {sat_name} (Area {sat['net_id']}, Net: {sat_cidr})")

        isis_cmd = [
            './configure-isis.sh',
            sat_name,
            sat['net_id'],
            *fixed_antennas,
            sat_cidr,  
            sat['host'],
            ssh_user
        ]
        
        try:
            subprocess.run(isis_cmd, check=True)
        except subprocess.CalledProcessError as e:
            print(f"❌ Failed to configure IS-IS for {sat_name}: {e}")

else:
    print("\n🚫 Skipping IS-IS Configuration (ENABLE_ISIS = False)")