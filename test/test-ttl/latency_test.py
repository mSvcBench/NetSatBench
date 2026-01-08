#!/usr/bin/env python3
"""
Latency and Hop experiment runner (ping over docker containers), driven by etcd config.

Example:
  ./latency_test.py \
    --etcd-host 10.0.1.215 --etcd-port 2379 \
    --output-dir test/test-ttl/latency_results \
    --prober-counts 1 2 4 \
    --iterations 3 \
    --ping-count 10
"""
import argparse
import csv
import etcd3
import json
import os
import random
import subprocess
import threading
import time
from typing import Dict, Any, List, Tuple
import matplotlib.pyplot as plt
import pandas as pd

# -----------------------------
# ETCD helpers
# -----------------------------
def get_prefix_data(etcd_client: etcd3.Etcd3Client, prefix: str) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for value, meta in etcd_client.get_prefix(prefix):
        key = meta.key.decode("utf-8").split("/")[-1]
        data[key] = json.loads(value.decode("utf-8"))
    return data

# -----------------------------
# Sampling helpers
# -----------------------------
def sample_unique_pairs(nodes: Dict[str, Any], k: int) -> List[Tuple[str, str]]:
    keys = list(nodes)
    if len(keys) < 2 * k:
        raise ValueError(f"Not enough nodes ({len(keys)}) to form {k} non-overlapping pairs.")

    selected: set[Tuple[str, str]] = set()
    used: set[str] = set()
    attempts = 0
    max_attempts = 10000

    while len(selected) < k and attempts < max_attempts:
        a, b = random.sample(keys, 2)
        if a in used or b in used:
            attempts += 1
            continue
        pair = tuple(sorted((a, b)))
        selected.add(pair)
        used.update(pair)
        attempts += 1

    if len(selected) < k:
        raise ValueError(f"Failed to sample {k} pairs after {max_attempts} attempts.")

    return list(selected)

# -----------------------------
# Ping test logic
# -----------------------------
def run_ping_test(
    idx: int, 
    src: str, 
    dst: str, 
    prober_count: int, 
    ping_count: int, 
    workers: Dict, 
    all_nodes: Dict, 
    results: List, 
    lock: threading.Lock,
 ) -> None:
    
    """Run ping test from src to dst and record TTL and average delay."""
    
    src_worker = workers.get(all_nodes[src].get("worker"))
    dst_worker = workers.get(all_nodes[dst].get("worker"))
    if not src_worker or not dst_worker:
        with lock:
            results.append(
                {
                    "prober_count": prober_count,
                    "prober_id": idx + 1,
                    "src": src,
                    "dst": dst,
                    "ttl": "N/A",
                    "hop_count": "N/A",
                    "avg_delay_ms": "N/A",
                    "status": "Failed - Missing Worker"
                }
            )
        print(f"    ⚠️ {src} → {dst}: missing worker mapping")
        return
    # Execute ping command via SSH
    cmd = ["ssh", f"{src_worker['ssh_user']}@{src_worker['ip']}", "docker", "exec", src, "ping", "-c", str(ping_count), dst]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout, _ = proc.communicate()
    output = stdout.decode("utf-8")

    ttl_received = None
    avg_delay = "N/A"

    # 1. Parse TTL for Hop Calculation
    for line in output.splitlines():
        if "ttl=" in line.lower():
            try:
                ttl_str = line.lower().split("ttl=")[1].split()[0]
                ttl_received = int(ttl_str)
            except: pass
            break

    # Calculate Hops: Initial 64 (Linux Default) - Received
    hops = (64 - ttl_received) if ttl_received is not None else "N/A"

    # 2. Parse Average Latency (Delay)
    for line in output.splitlines():
        if "rtt min/avg/max/mdev" in line.lower():
            try:
                stats = line.split("=")[1].strip().split("/")
                if len(stats) >= 2:
                    avg_delay = stats[1]
            except: pass
            break

    # 3. Store Results
    with lock:
        results.append({
            "prober_count": prober_count,
            "prober_id": idx + 1,
            "src": src,
            "dst": dst,
            "ttl": ttl_received if ttl_received else "N/A",
            "hop_count": hops,
            "avg_delay_ms": avg_delay,
            "status": "Success" if ttl_received else "Failed"
        })
    
    if ttl_received:
        print(f"    ✅ {src} → {dst}: {hops} hops, {avg_delay} ms")
    else:
        print(f"    ❌ {src} → {dst}: Ping Failed")

# -----------------------------
# Output helpers
# -----------------------------
def write_csv(results: List[Dict[str, Any]], csv_path: str) -> None:
    # New Fieldnames excluding throughput
    fieldnames = ["prober_count", "prober_id", "src", "dst", "ttl", "hop_count", "avg_delay_ms", "status"]
    with open(csv_path, "w", newline="") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        for row in results:
            writer.writerow(row)
     
# plot ltt in             
def plot_summary(summary_csv: str, output_png: str) -> None:
    """Reads the CSV and creates a trend plot of Latency vs Load."""
    if not os.path.exists(summary_csv):
        print(f"    ⚠️ CSV {summary_csv} not found. Skipping plot.")
        return

    df = pd.read_csv(summary_csv)
    # Filter only successful pings
    df_success = df[df["status"] == "Success"].copy()
    
    # Convert types for calculation
    df_success["avg_delay_ms"] = pd.to_numeric(df_success["avg_delay_ms"], errors='coerce')
    df_success["hop_count"] = pd.to_numeric(df_success["hop_count"], errors='coerce')

    # Aggregating data: Mean Latency per Prober Count
    summary = df_success.groupby("prober_count")["avg_delay_ms"].mean().reset_index()

    plt.figure(figsize=(10, 6))
    
    # Plot 1: Average Latency Trend
    plt.plot(summary["prober_count"], summary["avg_delay_ms"], marker='o', linestyle='-', color='b', label='Avg Latency')
    
    # Optional: Scatter individual points to show variance
    plt.scatter(df_success["prober_count"], df_success["avg_delay_ms"], alpha=0.3, color='gray', label='Individual Tests')

    plt.xlabel("Number of Concurrent Probers")
    plt.ylabel("Average Latency (ms)")
    plt.title("Network Latency Performance under Concurrent Load")
    plt.legend()
    plt.grid(True, linestyle='--', alpha=0.7)
    plt.savefig(output_png)
    plt.close()
    print(f" Plot saved to {output_png}")
# -----------------------------
# Main
# -----------------------------
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run Latency and Hop tests over docker containers.")
    
    p.add_argument(
        "--etcd-host",
        default=os.getenv("ETCD_HOST", "127.0.0.1"),
        help="Etcd host (default: env ETCD_HOST or 127.0.0.1)",
    )
    p.add_argument(
        "--etcd-port",
        type=int,
        default=int(os.getenv("ETCD_PORT", 2379)),
        help="Etcd port (default: env ETCD_PORT or 2379)",
    )
    p.add_argument(
        "--etcd-user",
        default=os.getenv("ETCD_USER", None ),
        help="Etcd user (default: env ETCD_USER or None)",
    )
    p.add_argument(
        "--etcd-password",
        default=os.getenv("ETCD_PASSWORD", None ),
        help="Etcd password (default: env ETCD_PASSWORD or None)",
    )
    p.add_argument(
        "--etcd-ca-cert",
        default=os.getenv("ETCD_CA_CERT", None ),
        help="Path to Etcd CA certificate (default: env ETCD_CA_CERT or None)",
    )
    p.add_argument("--output-dir", default="test/test-ttl/latency_results", help="Output directory for logs/CSV/plot-latancy")
    p.add_argument("--prober-counts", type=int, nargs="+", default=[1])
    p.add_argument("--iterations", type=int, default=3)
    p.add_argument("--ping-count", type=int, default=10)
    p.add_argument("--seed", type=int, default=None)
    return p.parse_args()

def main() -> None:
    args = parse_args()
    os.makedirs(args.output_dir, exist_ok=True)

    etcd = etcd3.client(host=args.etcd_host, port=args.etcd_port)

    # Load Configuration
    satellites = get_prefix_data(etcd, "/config/satellites/")
    grounds = get_prefix_data(etcd, "/config/grounds/")
    users = get_prefix_data(etcd, "/config/users/")
    workers = get_prefix_data(etcd, "/config/workers/")
    all_nodes = {**satellites, **grounds, **users}

    results: List[Dict[str, Any]] = []
    lock = threading.Lock()

    if args.seed is not None:
        random.seed(args.seed)

    for prober_count in args.prober_counts:
        print(f"\n⏱ Starting {prober_count} parallel ping probers...")
        for it in range(args.iterations):
            print(f" ▶️ Iteration {it+1}/{args.iterations}")
            pairs = sample_unique_pairs(all_nodes, prober_count)
            
            threads: List[threading.Thread] = []
            for idx, (src, dst) in enumerate(pairs):
                th = threading.Thread(
                    target=run_ping_test,
                    args=(idx, 
                          src,
                          dst, 
                          prober_count, 
                          args.ping_count, 
                          workers, 
                          all_nodes, 
                          results, 
                          lock),
                    daemon=False,
                )
                threads.append(th)
                th.start()

            for th in threads:
                th.join()

    # Save to the new CSV name
    csv_path = os.path.join(args.output_dir, "latency_results.csv")
    write_csv(results, csv_path)

    plot_path = os.path.join(args.output_dir, "latency_plot.png")
    plot_summary(summary_csv=csv_path, output_png=plot_path)

    print(f"\n📊 Experiment Finished. Results written to {csv_path} and {plot_path}")

if __name__ == "__main__":
    main()