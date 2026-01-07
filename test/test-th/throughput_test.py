#!/usr/bin/env python3
"""
Throughput experiment runner (iperf3 over docker containers), driven by etcd config.

Example:
  ./throughput_test.py \
    --etcd-host 10.0.1.215 --etcd-port 2379 \
    --output-dir test/test-1/throughput_results \
    --prober-counts 1 2 4 \
    --iterations 3 \
    --duration 30 \
    --parallel-streams 40
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


# -----------------------------
# ETCD helpers
# -----------------------------
def get_prefix_data(etcd_client: etcd3.Etcd3Client, prefix: str) -> Dict[str, Any]:
    """
    Fetch all key-value pairs under an etcd prefix.
    Keys are truncated to the last path segment (after the final '/').
    Values are parsed as JSON.
    """
    data: Dict[str, Any] = {}
    for value, meta in etcd_client.get_prefix(prefix):
        key = meta.key.decode("utf-8").split("/")[-1]
        data[key] = json.loads(value.decode("utf-8"))
    return data


# -----------------------------
# Sampling helpers
# -----------------------------
def sample_unique_pairs(nodes: Dict[str, Any], k: int, seed: int | None = None) -> List[Tuple[str, str]]:
    """
    Sample k *pairs* (src, dst) such that:
      - within each pair src != dst
      - no node is reused across pairs (non-overlapping endpoints)

    Raises ValueError if k pairs cannot be formed.
    """
    if seed is not None:
        random.seed(seed)

    keys = list(nodes)
    if len(keys) < 2 * k:
        raise ValueError(f"Not enough nodes ({len(keys)}) to form {k} non-overlapping pairs.")

    selected: set[Tuple[str, str]] = set()
    used: set[str] = set()

    # Try a reasonable number of attempts before failing
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
        raise ValueError(f"Failed to sample {k} non-overlapping pairs after {max_attempts} attempts.")

    return list(selected)


# -----------------------------
# Iperf runner
# -----------------------------
def run_iperf_test(
    idx: int,
    src: str,
    dst: str,
    prober_count: int,
    duration: int,
    parallel_streams: int,
    workers: Dict[str, Any],
    all_nodes: Dict[str, Any],
    output_dir: str,
    results: List[Dict[str, Any]],
    lock: threading.Lock,
) -> None:
    """
    Run one iperf3 test src -> dst using ssh->docker exec.
    Stores results into shared 'results' list under lock.
    """
    src_worker = workers.get(all_nodes[src].get("worker"))
    dst_worker = workers.get(all_nodes[dst].get("worker"))

    if not src_worker or not dst_worker:
        with lock:
            results.append(
                {
                    "prober_count": prober_count,
                    "prober": idx + 1,
                    "src": src,
                    "dst": dst,
                    "throughput_mbps": 0.0,
                    "error": "Missing worker mapping for src/dst",
                }
            )
        print(f"    ⚠️ {src} → {dst}: missing worker mapping")
        return

    # Cleanup any existing iperf3 processes
    subprocess.run(
        ["ssh", f"{src_worker['ssh_user']}@{src_worker['ip']}", "docker", "exec", src, "pkill", "-f", "iperf3"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["ssh", f"{dst_worker['ssh_user']}@{dst_worker['ip']}", "docker", "exec", dst, "pkill", "-f", "iperf3"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Start iperf3 server on dst
    subprocess.Popen(["ssh", f"{dst_worker['ssh_user']}@{dst_worker['ip']}", "docker", "exec", dst, "iperf3", "-s", "-D"])
    time.sleep(2)

    # Run iperf3 client from src
    cmd = [
        "ssh",
        f"{src_worker['ssh_user']}@{src_worker['ip']}",
        "docker",
        "exec",
        src,
        "iperf3",
        "-c",
        dst,
        "-t",
        str(duration),
        "-P",
        str(parallel_streams),
        "--json",
    ]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout, stderr = proc.communicate()

    try:
        output = json.loads(stdout.decode("utf-8"))
        sum_sent = output.get("end", {}).get("sum_sent")
        if not sum_sent or "bits_per_second" not in sum_sent:
            raise ValueError("Missing end.sum_sent.bits_per_second in iperf3 output")

        mbps = float(sum_sent["bits_per_second"]) / 1e6
        print(f"    ✅ {src} → {dst}: {mbps:.2f} Mbps")

        log_dir = os.path.join(output_dir, "iperf_logs")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, f"{src}_to_{dst}_prober{prober_count}.json")
        with open(log_path, "w") as f:
            json.dump(output, f, indent=2)

        with lock:
            results.append(
                {
                    "prober_count": prober_count,
                    "prober": idx + 1,
                    "src": src,
                    "dst": dst,
                    "throughput_mbps": mbps,
                }
            )
    except Exception as e:
        print(f"    ⚠️ Prober {idx+1} failed ({src} → {dst}): {e}")

        err_path = os.path.join(output_dir, f"error_prober_{idx+1}_probercount_{prober_count}.log")
        with open(err_path, "wb") as f:
            f.write(stderr)

        with lock:
            results.append(
                {
                    "prober_count": prober_count,
                    "prober": idx + 1,
                    "src": src,
                    "dst": dst,
                    "throughput_mbps": 0.0,
                    "error": str(e),
                }
            )


# -----------------------------
# Output helpers
# -----------------------------
def write_csv(results: List[Dict[str, Any]], csv_path: str) -> None:
    fieldnames = ["prober_count", "prober", "src", "dst", "throughput_mbps", "error"]
    with open(csv_path, "w", newline="") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        for row in results:
            if "error" not in row:
                row = {**row, "error": ""}
            writer.writerow(row)


def compute_summary(results: List[Dict[str, Any]], prober_counts: List[int]) -> Dict[int, float]:
    summary: Dict[int, float] = {}
    for pc in prober_counts:
        vals = [r["throughput_mbps"] for r in results if r.get("prober_count") == pc and isinstance(r.get("throughput_mbps"), (int, float))]
        summary[pc] = (sum(vals) / len(vals)) if vals else 0.0
    return summary


def plot_summary(summary: Dict[int, float], plot_path: str) -> None:
    x_vals = sorted(summary.keys())
    y_vals = [summary[k] for k in x_vals]

    plt.figure(figsize=(10, 6))
    plt.plot(x_vals, y_vals, marker="o")
    plt.title("Average Throughput vs Number of Probers")
    plt.xlabel("Number of Probers")
    plt.ylabel("Average Throughput (Mbps)")
    plt.grid(True)
    plt.tight_layout()
    plt.savefig(plot_path)


# -----------------------------
# Main
# -----------------------------
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run iperf3 throughput tests over docker containers, using etcd config.")

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

    p.add_argument("--output-dir", default="test/test-1/throughput_results", help="Output directory for logs/CSV/plot")

    p.add_argument("--prober-counts", type=int, nargs="+", default=[1], help="List of prober counts to test (e.g., 1 2 4)")
    p.add_argument("--iterations", type=int, default=1, help="Iterations per prober count")
    p.add_argument("--duration", type=int, default=30, help="iperf3 duration in seconds")
    p.add_argument("--parallel-streams", type=int, default=16, help="iperf3 -P parallel streams")

    p.add_argument("--seed", type=int, default=None, help="Random seed for reproducibility")


    return p.parse_args()


def main() -> None:
    args = parse_args()

    os.makedirs(args.output_dir, exist_ok=True)
    os.makedirs(os.path.join(args.output_dir, "iperf_logs"), exist_ok=True)

    etcd = etcd3.client(host=args.etcd_host, port=args.etcd_port)

    # Load config
    satellites = get_prefix_data(etcd, "/config/satellites/")
    grounds = get_prefix_data(etcd, "/config/grounds/")
    users = get_prefix_data(etcd, "/config/users/")
    workers = get_prefix_data(etcd, "/config/workers/")
    all_nodes = {**satellites, **grounds, **users}

    results: List[Dict[str, Any]] = []
    lock = threading.Lock()

    for prober_count in args.prober_counts:
        print(f"\n⏱ Running test with {prober_count} probers...")

        for it in range(args.iterations):
            print(f" ▶️ Iteration {it+1}/{args.iterations}")

            pairs = sample_unique_pairs(all_nodes, prober_count, seed=args.seed)

            threads: List[threading.Thread] = []
            for idx, (src, dst) in enumerate(pairs):
                print(f"  Prober {idx+1}: {src} → {dst}")
                th = threading.Thread(
                    target=run_iperf_test,
                    args=(
                        idx,
                        src,
                        dst,
                        prober_count,
                        args.duration,
                        args.parallel_streams,
                        workers,
                        all_nodes,
                        args.output_dir,
                        results,
                        lock,
                    ),
                    daemon=False,
                )
                threads.append(th)
                th.start()

            for th in threads:
                th.join()

        # Average for this prober_count over all completed tests
        vals = [r["throughput_mbps"] for r in results if r.get("prober_count") == prober_count]
        avg_total = (sum(vals) / len(vals)) if vals else 0.0
        print(f"  ✅ Average throughput ({prober_count} probers): {avg_total:.2f} Mbps")

    # Save CSV + plot
    csv_path = os.path.join(args.output_dir, "throughput_results.csv")
    write_csv(results, csv_path)

    summary = compute_summary(results, args.prober_counts)
    plot_path = os.path.join(args.output_dir, "throughput_plot.png")
    plot_summary(summary, plot_path)

    print(f"\n📊 Finished. Results written to {csv_path} and {plot_path}")


if __name__ == "__main__":
    main()
