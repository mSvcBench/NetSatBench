#!/usr/bin/env python3
import etcd3
import subprocess
import json
import os
import sys
import re

# ==========================================
# 🚩 CONFIGURATION
# ==========================================
# get ETCD_HOST, ETCD_PORT and SAT_HOST_BRIDGE_NAME from environment variables if set
ETCD_HOST = os.getenv('ETCD_HOST', '127.0.0.1')
ETCD_PORT = int(os.getenv('ETCD_PORT', 2379))

try:
    etcd = etcd3.client(host=ETCD_HOST, port=ETCD_PORT)
except Exception as e:
    print(f"❌ Failed to initialize Etcd client: {e}")
    sys.exit(1)

# ==========================================
# INJECT CONFIGURATION IN ETCD    
# ==========================================
## load json from file host-config.json and apply to Etcd

filename = os.path.basename("config.json")
try:
    # Open in r+ mode to allow reading AND writing back to the same file
    with open(filename, "r+", encoding="utf-8") as f:
        config = json.load(f)
        file_modified = False

        # --- 3. ETCD SYNC ---
        allowed_keys = ["satellites", "users", "grounds", "L3-config", "hosts", "epoch-config"]

        # A. Push General Config & Inventory
        for key, value in config.items():
            if key not in allowed_keys:
                # the key should not be present in epoch file, skip it
                print(f"❌ [{filename}] Unexpected key '{key}' found in epoch file, skipping...")
                continue
            if key in ["satellites", "users", "grounds"]:
                for k, v in value.items():
                    etcd.put(f"/config/{key}/{k}", json.dumps(v))

    print(f"✅ Successfully applied {filename} to Etcd.")
except FileNotFoundError:
    print(f"❌ Error: File {filename} not found.")
    sys.exit(1)
except json.JSONDecodeError as e:
    print(f"❌ Error: Failed to parse JSON in {filename}: {e}")
    sys.exit(1)

