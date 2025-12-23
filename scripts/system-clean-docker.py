#!/usr/bin/env python3
"""
system-clean-docker.py

Undo what your "configure host" script did:
  1) Remove all-to-all routes among hosts (ip route del ...)
  2) Remove the DOCKER-USER ACCEPT rule you inserted
  3) Remove the remote Docker network (sat-vnet)
  4) Remove the ETCD keys that your script created/overwrote

Linux-only (remote hosts) and uses ssh + subprocess, same style as your script.

Usage:
  ./teardown_hosts.py                 # uses ./config.json
  ./teardown_hosts.py --config foo.json
  ./teardown_hosts.py --dry-run
"""

import argparse
import etcd3
import json
import os
import subprocess
import sys


# --------------------------
# ETCD
# --------------------------
ETCD_HOST = os.getenv("ETCD_HOST", "127.0.0.1")
ETCD_PORT = int(os.getenv("ETCD_PORT", "2379"))


# --------------------------
# Helpers
# --------------------------
def run(cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        shell=True,
        executable="/bin/bash",
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def ssh(remote: str, cmd: str) -> str:
    # remote must already include "user@ip -i key" like your original code
    return f"ssh -o StrictHostKeyChecking=no {remote} '{cmd}'"


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_prefix_data(etcd_client, prefix: str) -> dict:
    data = {}
    for value, metadata in etcd_client.get_prefix(prefix):
        key = metadata.key.decode("utf-8").split("/")[-1]
        try:
            data[key] = json.loads(value.decode("utf-8"))
        except json.JSONDecodeError:
            # keep raw if not JSON
            data[key] = value.decode("utf-8", "replace")
    return data


def iptables_delete_rule_loop(remote: str, rule_check: str, rule_delete: str, dry_run: bool) -> None:
    """
    Repeatedly delete a rule as long as it exists (covers duplicates).
    rule_check: command used with iptables -C ...
    rule_delete: command used with iptables -D ...
    """
    while True:
        check_cmd = ssh(remote, rule_check)
        chk = run(check_cmd)
        if chk.returncode != 0:
            break  # rule not present
        del_cmd = ssh(remote, rule_delete)
        if dry_run:
            print(f"[DRY-RUN] {del_cmd}")
            break
        out = run(del_cmd)
        if out.returncode != 0:
            # if deletion failed, stop to avoid infinite loop
            print(f"⚠️  Failed deleting iptables rule.\nCMD: {del_cmd}\nSTDERR:\n{out.stderr}")
            break


def best_effort(remote: str, cmd: str, dry_run: bool) -> None:
    full = ssh(remote, cmd)
    if dry_run:
        print(f"[DRY-RUN] {full}")
        return
    res = run(full)
    if res.returncode != 0:
        # "best effort": print warning but continue
        msg = (res.stderr or res.stdout).strip()
        if msg:
            print(f"⚠️  Command failed (continuing): {cmd}\n    {msg}")


# --------------------------
# Main teardown logic
# --------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Clean hosts of the constellation"
    )
    parser.add_argument(
        "-c", "--config",
        required=True,
        help="Path to the JSON configuration file (e.g., config.json)",
    )
    parser.add_argument(
        "--etcd-host",
        default=os.getenv("ETCD_HOST", "127.0.0.1"),
        help="Etcd host (default: env ETCD_HOST or 127.0.0.1)",
    )
    parser.add_argument(
        "--etcd-port",
        type=int,
        default=int(os.getenv("ETCD_PORT", 2379)),
        help="Etcd port (default: env ETCD_PORT or 2379)",
    )

    args = parser.parse_args()

    try:
        cfg = load_json(args.config)
    except FileNotFoundError:
        print(f"❌ Error: File {args.config} not found.")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"❌ Error: Failed to parse JSON in {args.config}: {e}")
        sys.exit(1)

    try:
        etcd = etcd3.client(host=ETCD_HOST, port=ETCD_PORT)
    except Exception as e:
        print(f"❌ Failed to initialize Etcd client: {e}")
        sys.exit(1)

    # Read hosts from etcd (same as your script)
    hosts = get_prefix_data(etcd, "/config/hosts/")
    if not hosts:
        print("⚠️  No hosts found under /config/hosts/. Nothing to teardown on remote hosts.")
    else:
        # 1) Remove routes (all-to-all)
        for host_name, host in hosts.items():
            ssh_user = host.get("ssh_user", "ubuntu")
            ssh_ip = host.get("ip", host_name)
            ssh_key = host.get("ssh_key", "~/.ssh/id_rsa")
            remote = f"{ssh_user}@{ssh_ip} -i {ssh_key}"

            print(f"➞ Removing routes on host: {host_name} ({ssh_ip})")

            for other_name, other_host in hosts.items():
                if other_name == host_name:
                    continue
                other_ip = other_host.get("ip", other_name)
                other_cidr = other_host.get("sat-vnet-cidr", None)
                if not other_cidr:
                    continue

                # Your setup used: ip route replace <cidr> via <other_ip>
                # Teardown: delete that route (best-effort)
                best_effort(
                    remote,
                    f"sudo ip route del {other_cidr} via {other_ip}",
                    args.dry_run
                )

        # 2) Remove iptables DOCKER-USER ACCEPT rule (duplicates-safe)
        for host_name, host in hosts.items():
            ssh_user = host.get("ssh_user", "ubuntu")
            ssh_ip = host.get("ip", host_name)
            ssh_key = host.get("ssh_key", "~/.ssh/id_rsa")
            remote = f"{ssh_user}@{ssh_ip} -i {ssh_key}"

            sat_vnet_supercidr = host.get("SAT-VNET-SUPERNET", "172.0.0.0/8")

            print(f"➞ Removing DOCKER-USER forwarding rule on host: {host_name} ({ssh_ip})")
            # inserted rule was: -I DOCKER-USER -s super -d super -j ACCEPT
            # delete all matches:
            rule_check = f"sudo iptables -C DOCKER-USER -s {sat_vnet_supercidr} -d {sat_vnet_supercidr} -j ACCEPT"
            rule_delete = f"sudo iptables -D DOCKER-USER -s {sat_vnet_supercidr} -d {sat_vnet_supercidr} -j ACCEPT"
            iptables_delete_rule_loop(remote, rule_check, rule_delete, args.dry_run)

        # 3) Remove docker network on each host
        for host_name, host in hosts.items():
            ssh_user = host.get("ssh_user", "ubuntu")
            ssh_ip = host.get("ip", host_name)
            ssh_key = host.get("ssh_key", "~/.ssh/id_rsa")
            remote = f"{ssh_user}@{ssh_ip} -i {ssh_key}"

            sat_vnet = host.get("sat-vnet", "sat-vnet")

            print(f"➞ Removing docker network '{sat_vnet}' on host: {host_name} ({ssh_ip})")
            # best-effort: if it doesn't exist, continue
            best_effort(remote, f"docker network rm {sat_vnet}", args.dry_run)

    # 4) Remove ETCD keys that your script created/overwrote
    # Your script wrote:
    #   /config/L3-config/<k>
    #   /config/epoch-config
    #   /config/hosts/<host>
    #
    # We remove those prefixes/keys. (If you want to also wipe satellites/users/grounds,
    # uncomment the delete_prefix lines below.)
    print("➞ Removing ETCD configuration keys created by the setup script")

    if args.dry_run:
        print("[DRY-RUN] etcd.delete_prefix('/config/L3-config/')")
        print("[DRY-RUN] etcd.delete('/config/epoch-config')")
        print("[DRY-RUN] etcd.delete_prefix('/config/hosts/')")
    else:
        etcd.delete_prefix("/config/L3-config/")
        etcd.delete("/config/epoch-config")
        etcd.delete_prefix("/config/hosts/")

        # OPTIONAL (more destructive): wipe inventory too
        # etcd.delete_prefix("/config/satellites/")
        # etcd.delete_prefix("/config/users/")
        # etcd.delete_prefix("/config/grounds/")

    print("✅ Teardown completed.")


if __name__ == "__main__":
    main()
