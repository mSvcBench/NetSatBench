#!/usr/bin/env python3
import etcd3
import subprocess
import json
import os
import sys

# ==========================================
# 🚩 CONFIGURATION
# ==========================================
ETCD_HOST = '10.0.1.215'
ETCD_PORT = 2379
ENABLE_TC = True   # Enable Bandwidth/Latency rules
ENABLE_ISIS = True # Enable FRR/IS-IS configuration

try:
    etcd = etcd3.client(host=ETCD_HOST, port=ETCD_PORT)
except Exception as e:
    print(f"❌ Failed to initialize Etcd client: {e}")
    sys.exit(1)

def get_prefix_data(prefix):
    data = {}
    for value, metadata in etcd.get_prefix(prefix):
        key = metadata.key.decode('utf-8').split('/')[-1]
        try:
            data[key] = json.loads(value.decode('utf-8'))
        except json.JSONDecodeError:
            pass
    return data

def get_links_from_connection_key():
    value, _ = etcd.get('/config/links/connection')
    if value:
        return json.loads(value.decode('utf-8'))
    return []

# ==========================================
# LOAD DATA
# ==========================================
print(f"📡 Connecting to Etcd at {ETCD_HOST}:{ETCD_PORT}...")
satellites = get_prefix_data('/config/satellites/')
links = get_links_from_connection_key()
hosts = get_prefix_data('/config/hosts/')

print(f"   Found {len(satellites)} satellites, {len(links)} links.")

# ==========================================
# === STEP 4: Apply Traffic Control (TC) ===
# ==========================================
if ENABLE_TC:
    print("\n⚙️  Applying Traffic Control Rules...")
    
    for link in links:
        bw = link.get('bw')
        burst = link.get('burst')
        latency = link.get('latency')
        
        if not (bw and burst and latency):
            continue

        src_sat = link.get('src_sat')
        dst_sat = link.get('dst_sat')
        
        if src_sat not in satellites or dst_sat not in satellites:
            continue

        # Interface Naming Logic
        src_ant = link.get('src_antenna')
        dst_ant = link.get('dst_antenna')
        if_on_src = f"{dst_sat}_a{dst_ant}"
        if_on_dst = f"{src_sat}_a{src_ant}"

        # Host Info
        src_host = satellites[src_sat]['host']
        dst_host = satellites[dst_sat]['host']
        src_user = hosts.get(src_host, {}).get('ssh_user', 'ubuntu')
        dst_user = hosts.get(dst_host, {}).get('ssh_user', 'ubuntu')

        print(f"   🔹 {src_sat} <-> {dst_sat}: {bw}, {latency}")

        # Apply TC
        subprocess.run(['./apply-tc.sh', src_sat, if_on_src, src_host, src_user, bw, burst, latency], check=False)
        subprocess.run(['./apply-tc.sh', dst_sat, if_on_dst, dst_host, dst_user, bw, burst, latency], check=False)

else:
    print("\n🚫 Skipping Traffic Control (ENABLE_TC = False)")

# ==========================================
# === STEP 5: Configure IS-IS ===
# ==========================================
if ENABLE_ISIS:
    print("\n🚀 Configuring FRR/IS-IS...")

    isis_interfaces = {}

    for link in links:
        for sat_key in ['src_sat', 'dst_sat']:
            s = link.get(sat_key)
            if s: isis_interfaces[s] = True

    fixed_antennas = ['1', '2', '3', '4', '5']

    for sat_name in isis_interfaces.keys():
        if sat_name not in satellites: continue

        sat = satellites[sat_name]
        
        # ID Config
        sat_id_num = sat.get('ID', 0)
        net_id = f"{int(sat_id_num):04d}"
        
        # ⚡ FORCE NETWORK ADDRESS (Ends in .0/24) ⚡
        # 1. Get the raw string (e.g. "192.168.3.254/32" or just "192.168.3.254")
        raw_cidr = sat.get('sat_net_cidr', f"192.168.{sat_id_num}.0/24")
        
        # 2. Clean it to get just the IP part
        ip_part = raw_cidr.split('/')[0]
        
        # 3. Split into octets to ensure we get the Network Address (X.X.X.0)
        octets = ip_part.split('.')
        if len(octets) >= 3:
            # Reconstruct: Keep first 3 octets, force 4th to '0', append '/24'
            sat_cidr_24 = f"{octets[0]}.{octets[1]}.{octets[2]}.0/24"
        else:
            # Fallback if data is malformed
            sat_cidr_24 = f"192.168.{sat_id_num}.0/24"
        
        # Host/User
        host_key = sat.get('host', 'host-1')
        host_info = hosts.get(host_key, {})
        ssh_user = host_info.get('ssh_user', 'ubuntu')

        print(f"   📱 Configuring {sat_name} (NetID: {net_id}, CIDR: {sat_cidr_24})")

        isis_cmd = [
            './configure-isis.sh',
            sat_name,
            net_id,
            *fixed_antennas,
            sat_cidr_24,   # Now sending strict Network Address (e.g., 192.168.3.0/24)
            host_key,
            ssh_user
        ]

        try:
            subprocess.run(isis_cmd, check=True)
        except subprocess.CalledProcessError as e:
            print(f"   ❌ Error on {sat_name}: {e}")

print("\n✅ Configuration Complete.")