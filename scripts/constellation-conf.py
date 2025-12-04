import json
import os
from typing import Iterable, Dict, Any, List
import etcd3

# ==========================================
# 🚩 CONFIGURATION
# ==========================================
ETCD_HOST = "10.0.1.215"
ETCD_PORT = 2379
JSON_FILENAME = "data-test.json" 

PREFIXES_TO_WIPE: Iterable[str] = (
    "/config/hosts/",
    "/config/satellites/",
    "/config/users/",
    "/config/run/", 
)

# ==========================================
# 🧹 ETCD OPERATIONS
# ==========================================

def delete_prefix(etcd: etcd3.Etcd3Client, prefix: str) -> int:
    """Delete all keys under a given prefix."""
    count = 0
    try:
        for _, meta in etcd.get_prefix(prefix):
            key = meta.key.decode("utf-8")
            etcd.delete(key)
            count += 1
        print(f"🧹 Deleted {count} keys under prefix '{prefix}'")
    except Exception as e:
        print(f"❌ Error during deletion for prefix '{prefix}': {e}")
        
    return count

def sync_etcd_from_json(
    json_filename: str = JSON_FILENAME,
    host: str = ETCD_HOST,
    port: int = ETCD_PORT,
    wipe_prefixes: Iterable[str] = PREFIXES_TO_WIPE,
) -> None:
    """Synchronizes Etcd configuration from a JSON file."""
    
    script_dir = os.path.dirname(os.path.abspath(__file__))
    json_path = os.path.join(script_dir, json_filename)

    if not os.path.exists(json_path):
        print(f"❌ Error: Config file not found: {json_path}")
        return

    # 1. Load the JSON configuration
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            config: Dict[str, Any] = json.load(f)
    except json.JSONDecodeError as e:
        print(f"❌ Error decoding JSON from {json_path}: {e}")
        return
    except Exception as e:
        print(f"❌ Error reading file {json_path}: {e}")
        return

    # 2. Connect to Etcd
    etcd = etcd3.client(host=host, port=port)
    
    try:
        print(f"📡 Connecting to Etcd at {host}:{port}...")

        # 3. Wipe old prefixes (including /config/run/)
        for p in wipe_prefixes:
            delete_prefix(etcd, p)
            
        print("---")

        # 4. Upload Hosts
        for name, data in config.get("hosts", {}).items():
            etcd.put(f"/config/hosts/{name}", json.dumps(data))
            print(f" Stored host: {name}")

        # 5. Upload Satellites
        for name, data in config.get("satellites", {}).items():
            etcd.put(f"/config/satellites/{name}", json.dumps(data))
            print(f" Stored satellite: {name}")
            
        # 6. Upload Users
        for name, data in config.get("users", {}).items():
            etcd.put(f"/config/users/{name}", json.dumps(data))
            print(f" Stored user: {name}")
            
        print("---")

        # 7. Upload Links (ATOMIC OVERWRITE)
        links: List[Dict[str, Any]] = config.get("links", [])
        etcd.put("/config/links", json.dumps(links))
        print(f"✅ Stored {len(links)} links under /config/links (Atomic Update)")

        # 8. Upload Run Commands (NEW STEP)
        run_commands_count = 0
        for name, commands in config.get("run", {}).items():
            # Commands are stored as a JSON array under /config/run/{name}
            etcd.put(f"/config/run/{name}", json.dumps(commands))
            print(f" Stored run commands for: {name}")
            run_commands_count += 1
        print(f"✅ Stored commands for {run_commands_count} entities under /config/run/")
        
        print("\n🎉 Sync complete.")

    except etcd3.exceptions.Etcd3Exception as e:
        print(f"❌ Etcd Connection Error: Check if etcd is running at {host}:{port}. Error: {e}")
    except Exception as e:
        print(f"❌ Critical Error: {e}")
    finally:
        etcd.close()

if __name__ == "__main__":
    sync_etcd_from_json()