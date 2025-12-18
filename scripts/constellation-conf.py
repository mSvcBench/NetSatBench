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
ETCD_HOST = "10.0.1.215"
ETCD_PORT = 2379
EPOCH_DIR = "constellation-epochs"
FILE_PATTERN = "NetSatBench-epoch*.json"

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
            file_modified = False

            # --- WAIT FOR SCHEDULED TIME ---
            # Wait happens here so we are ready to push exactly on time
            smart_wait(config.get("epoch-time"), filename)

            # --- 3. ETCD SYNC ---
            special_keys = ["epoch-time", "links-add", "link-delete", "link-update", "run", "L3-config", "hosts", "satellites", "users"]

            # A. Push General Config & Inventory
            for key, value in config.items():
                if key not in special_keys:
                    etcd.put(f"/config/{key}", json.dumps(value))
                    continue
                
                # Handle nested dictionaries for specific keys
                if key == "L3-config":
                    for k, v in value.items():
                        etcd.put(f"/config/L3-config/{k}", str(v).strip().replace('"', ''))
                elif key in ["hosts", "satellites", "users"]:
                    for k, v in value.items():
                        etcd.put(f"/config/{key}/{k}", json.dumps(v))


            # B. Push Dynamic Actions (Now containing corrected VNIs)
            add = config.get("links-add", [])
            delete = config.get("link-delete", [])
            update = config.get("link-update", [])

            for l in add:
                endpoint1 = l["endpoint1"]
                endpoint2 = l["endpoint2"]
                vni = calculate_vni(l["endpoint1"], l["endpoint1_antenna"], 
                                            l["endpoint2"], l["endpoint2_antenna"])
                l["vni"] = vni
                print(f"🏗️  [{filename}] Syncing /config/links-add for {endpoint1} - {endpoint2} with VNI {vni}")
                etcd.put(f"/config/links/{endpoint1}/{vni}", json.dumps(l))
                etcd.put(f"/config/links/{endpoint2}/{vni}", json.dumps(l))
            for l in delete:
                endpoint1 = l["endpoint1"]
                endpoint2 = l["endpoint2"]
                vni = calculate_vni(l["endpoint1"], l["endpoint1_antenna"], 
                                            l["endpoint2"], l["endpoint2_antenna"])
                l["vni"] = vni
                print(f"🗑️  [{filename}] Syncing /config/link-delete for {endpoint1} - {endpoint2} with VNI {vni}")
                etcd.delete(f"/config/links/{endpoint1}/{vni}")
                etcd.delete(f"/config/links/{endpoint2}/{vni}")
            for l in update:
                endpoint1 = l["endpoint1"]
                endpoint2 = l["endpoint2"]
                vni = calculate_vni(l["endpoint1"], l["endpoint1_antenna"], 
                                            l["endpoint2"], l["endpoint2_antenna"])
                l["vni"] = vni
                print(f"♻️  [{filename}] Syncing /config/link-update for {endpoint1} - {endpoint2} with VNI {vni}")
                etcd.put(f"/config/links/{endpoint1}/{vni}", json.dumps(l))
                etcd.put(f"/config/links/{endpoint2}/{vni}", json.dumps(l))

            # D. Push Runtime Commands
            for node, cmds in config.get("run", {}).items():
                etcd.put(f"/config/run/{node}", json.dumps(cmds))

            print(f"✅ [{filename}] Epoch applied successfully.")

            # --- 4. FILE UPDATE (Strictly Last) ---
            # If we fixed any VNIs, save the changes back to the JSON file now.
            if file_modified:
                print(f"💾 [{filename}] Updating JSON file with corrected VNIs...")
                f.seek(0)
                json.dump(config, f, indent=2)
                f.truncate() # Removes any leftover data if new file is smaller

    except Exception as e:
        print(f"❌ Error processing {filename}: {e}")

# ==========================================
# 🏁 RUNNER
# ==========================================
def run_all_epochs():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    search_path = os.path.join(script_dir, EPOCH_DIR, FILE_PATTERN)
    
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