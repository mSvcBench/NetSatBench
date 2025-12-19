import json
import os
import time
import glob
import re
import etcd3
import zlib

# ==========================================
# 🚩 CONFIGURATION
# ==========================================
# get ETCD_HOST and ETCD_PORT from environment variables if set
ETCD_HOST = os.getenv('ETCD_HOST', '127.0.0.1')
ETCD_PORT = int(os.getenv('ETCD_PORT', 2379))

TIME_OFFSET = None

# ==========================================
# 🧮 HELPERS
# ==========================================
def calculate_vni(ep1, ant1, ep2, ant2):
    """
    Generates a deterministic VNI based on endpoints and antennas.
    """
    # Sort endpoints to ensure A->B and B->A produce the same VNI
    if str(ep1) < str(ep2):
        val = f"{ep1}_{ant1}_{ep2}_{ant2}"
    else:
        val = f"{ep2}_{ant2}_{ep1}_{ant1}"
    
    # Calculate CRC32 checksum
    checksum = zlib.crc32(val.encode('utf-8'))
    
    # Return a positive integer within standard VNI range constraints
    return (checksum % 16777215) + 1

def smart_wait(target_virtual_time_str, filename):
    """
    Syncs the emulation virtual time with real wall-clock time.
    """
    global TIME_OFFSET
    if not target_virtual_time_str: return
    try:
        virtual_time = float(target_virtual_time_str)
        current_real_time = time.time()
        
        # Initialize baseline on the first epoch
        if TIME_OFFSET is None:
            TIME_OFFSET = current_real_time - virtual_time
            print(f"⏱️  [{filename}] Baseline set. Virtual Time: {virtual_time}")
            return
            
        target_real_time = virtual_time + TIME_OFFSET
        delay = target_real_time - time.time()
        
        if delay > 0:
            print(f"⏳ [{filename}] Waiting {delay:.1f}s to sync epoch...")
            time.sleep(delay)
    except ValueError: 
        print(f"⚠️ [{filename}] Invalid time format: {target_virtual_time_str}")

# ==========================================
# 🚀 CORE LOGIC
# ==========================================
def apply_single_epoch(json_path, etcd):
    filename = os.path.basename(json_path)
    try:
        # Open in r+ mode to allow reading AND writing back to the same file
        with open(json_path, "r+", encoding="utf-8") as f:
            config = json.load(f)

            # --- WAIT FOR SCHEDULED TIME ---
            # Wait happens here so we are ready to push exactly on time
            smart_wait(config.get("epoch-time"), filename)

            # --- 3. ETCD SYNC ---
            allowed_keys = ["epoch-time", "links-add", "links-del", "links-update", "run", "satellites", "users", "grounds", "time"]

            # A. Push satellites, users, grounds, run and epoch-time keys in etcd
            for key, value in config.items():
                if key not in allowed_keys:
                    # the key should not be present in epoch file, skip it
                    print(f"❌ [{filename}] Unexpected key '{key}' found in epoch file, skipping...")
                    continue
                if key == "epoch-time":
                    etcd.put(f"/config/epoch-time", str(value).strip().replace('"', ''))
                elif key in ["satellites", "users", "grounds","run"]:
                    for k, v in value.items():
                        etcd.put(f"/config/{key}/{k}", json.dumps(v))


            # B. Push Dynamic Actions (Now containing corrected VNIs)
            add = config.get("links-add", [])
            delete = config.get("link-delete", [])
            update = config.get("link-update", [])
                
            for l in add:
                # Process additions
                vxlan_iface_name1 = f"{l['endpoint2']}_a{l['endpoint2_antenna']}"
                vxlan_iface_name2 = f"{l['endpoint1']}_a{l['endpoint1_antenna']}"
                etcd_key1 = f"/config/links/{l['endpoint1']}/{vxlan_iface_name1}"
                etcd_key2 = f"/config/links/{l['endpoint2']}/{vxlan_iface_name2}"
                vni = calculate_vni(l["endpoint1"], l["endpoint1_antenna"], 
                                                l["endpoint2"], l["endpoint2_antenna"])
                l["vni"] = vni
                print(f"🪢  [{filename}] Syncing /config/links-add for {l['endpoint1']} - {l['endpoint2']} with VNI {vni}")
                etcd.put(etcd_key1, json.dumps(l))
                etcd.put(etcd_key2, json.dumps(l))
            for l in delete:
                vxlan_iface_name1 = f"{l['endpoint2']}_a{l['endpoint2_antenna']}"
                vxlan_iface_name2 = f"{l['endpoint1']}_a{l['endpoint1_antenna']}"
                etcd_key1 = f"/config/links/{l['endpoint1']}/{vxlan_iface_name1}"
                etcd_key2 = f"/config/links/{l['endpoint2']}/{vxlan_iface_name2}"
                print(f"✂️  [{filename}] Syncing /config/link-delete for {l['endpoint1']} - {l['endpoint2']} with VNI {vni}")
                etcd.delete(etcd_key1)
                etcd.delete(etcd_key2)
            for l in update:
                vxlan_iface_name1 = f"{l['endpoint2']}_a{l['endpoint2_antenna']}"
                vxlan_iface_name2 = f"{l['endpoint1']}_a{l['endpoint1_antenna']}"
                etcd_key1 = f"/config/links/{l['endpoint1']}/{vxlan_iface_name1}"
                etcd_key2 = f"/config/links/{l['endpoint2']}/{vxlan_iface_name2}"
                # sanity check: ensure the link exists before updating
                etcd_value1, _ = etcd.get(etcd_key1)
                etcd_value2, _ = etcd.get(etcd_key2)
                if not etcd_value1 or not etcd_value2:
                    print(f"⚠️  [{filename}] Link not found in Etcd for {l['endpoint1']} - {l['endpoint2']}. Skipping update.")
                    continue
                print(f"♻️  [{filename}] Syncing /config/link-update for {l['endpoint1']} - {l['endpoint2']}")
                etcd.put(etcd_key1, json.dumps(l))
                etcd.put(etcd_key2, json.dumps(l))

            # D. Push Runtime Commands
            for node, cmds in config.get("run", {}).items():
                etcd.put(f"/config/run/{node}", json.dumps(cmds))

            print(f"✅ [{filename}] Epoch applied successfully.")

    except Exception as e:
        print(f"❌ Error processing {filename}: {e}")

# ==========================================
# 🏁 RUNNER
# ==========================================
def run_all_epochs():
    # Load configuration for epoch files from etcd key (epoch-config)
    try:
        etcd = etcd3.client(host=ETCD_HOST, port=ETCD_PORT)
        epoch_config_value, _ = etcd.get('/config/epoch-config')
        if epoch_config_value:
            epoch_config = json.loads(epoch_config_value.decode('utf-8'))
            EPOCH_DIR = epoch_config.get('EPOCH_DIR', 'constellation-epochs')
            FILE_PATTERN = epoch_config.get('FILE_PATTERN', 'NetSatBench-epoch*.json')
        else:
            EPOCH_DIR = "constellation-epochs"
            FILE_PATTERN = "NetSatBench-epoch*.json"
    except Exception as e:
        print(f"❌ Failed to load epoch configuration from Etcd: {e}")
        return

    search_path = os.path.join(EPOCH_DIR, FILE_PATTERN)
    
    # Sort files by epoch number (e.g., epoch1, epoch2, ...)
    files = sorted(glob.glob(search_path), key=lambda x: int(re.search(r'epoch(\d+)', x).group(1) or 0))
    
    if not files:
        print(f"⚠️ No epoch files found in {search_path}")
        return

    print(f"🚀 Starting emulation with {len(files)} epochs found.")
    
    try:
        etcd = etcd3.client(host=ETCD_HOST, port=ETCD_PORT)
        # Test connection
        etcd.status() 
    except Exception as e:
        print(f"❌ Could not connect to Etcd at {ETCD_HOST}:{ETCD_PORT}. Is it running?")
        print(f"Details: {e}")
        return

    for f in files:
        apply_single_epoch(f, etcd)

if __name__ == "__main__":
    run_all_epochs()