#!/usr/bin/env python3
"""
system-clean-docker.py

Undo what "system-init-docker.py" script did:
  1) Remove all-to-all routes among workers (ip route del ...)
  2) Remove the DOCKER-USER ACCEPT rule you inserted
  3) Remove the remote Docker network (sat-vnet)
  4) Remove the ETCD keys that your script created/overwrote
"""

import argparse
import etcd3
import json
import os
import subprocess
import sys
import logging

logging.basicConfig(level="INFO", format="[%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


# --------------------------
# Helpers
# --------------------------
def run(cmd: str) -> subprocess.CompletedProcess:
    """
    Run a shell command and return the CompletedProcess.
    Uses bash so you can pass a full command string.
    """
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


def iptables_delete_rule_loop(remote: str, rule_check: str, rule_delete: str) -> None:
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
        out = run(del_cmd)
        if out.returncode != 0:
            # if deletion failed, stop to avoid infinite loop
            log.warning(f"⚠️  Failed deleting iptables rule.\nCMD: {del_cmd}\nSTDERR:\n{out.stderr}")
            break


# --------------------------
# Main teardown logic
# --------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Clean hosts of the constellation"
    )
    parser.add_argument(
        "-c", "--config",
        default=None,
        help="Path to the JSON worker configuration file (default: None, data from Etcd if not provided)",
        required=False
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
    parser.add_argument(
        "--etcd-user",
        default=os.getenv("ETCD_USER", None ),
        help="Etcd user (default: env ETCD_USER or None)",
    )
    parser.add_argument(
        "--etcd-password",
        default=os.getenv("ETCD_PASSWORD", None ),
        help="Etcd password (default: env ETCD_PASSWORD or None)",
    )
    parser.add_argument(
        "--etcd-ca-cert",
        default=os.getenv("ETCD_CA_CERT", None ),
        help="Path to Etcd CA certificate (default: env ETCD_CA_CERT or None)",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level (default: INFO)",
    )

    args = parser.parse_args()
    log.setLevel(args.log_level.upper())

    cfg = {}
    if args.config:
        try:
            cfg = load_json(args.config)
        except FileNotFoundError:
            log.error(f"❌ Error: File {args.config} not found.")
            sys.exit(1)
        except json.JSONDecodeError as e:
            log.error(f"❌ Error: Failed to parse JSON in {args.config}: {e}")
            sys.exit(1)

    try:
        if args.etcd_user and args.etcd_password and args.etcd_ca_cert:
            etcd_client = etcd3.client(host=args.etcd_host, port=args.etcd_port, user=args.etcd_user, password=args.etcd_password, ca_cert=args.etcd_ca_cert)
        else:
            etcd_client = etcd3.client(host=args.etcd_host, port=args.etcd_port)
    except Exception as e:
        log.error(f"❌ Failed to initialize Etcd client: {e}")
        sys.exit(1)

    # Read hosts from etcd (same as your script)
    if cfg is not None and "workers" in cfg:
        workers = cfg["workers"]
    else:
        workers = get_prefix_data(etcd_client, "/config/workers/")
    if not workers:
        log.warning("⚠️  No workers found under /config/workers/. Nothing to teardown on remote workers.")
    else:
        # 1) Remove routes (all-to-all)
        for worker_name, worker in workers.items():
            ssh_user = worker.get("ssh-user", "ubuntu")
            ssh_ip = worker.get("ip", worker_name)
            ssh_key = worker.get("ssh-key", "~/.ssh/id_rsa")
            remote_str = f"{ssh_user}@{ssh_ip} -i {ssh_key}"
            sat_vnet = worker.get("sat-vnet", "sat-vnet")
            sat_vnet_supercidr = worker.get("sat-vnet-super-cidr", "172.0.0.0/8")
            log.info(f"🧹 Cleaning worker {worker_name} at {ssh_ip}")
            
            # Verify connectivity
            try:
                subprocess.run(f"ssh -o StrictHostKeyChecking=no -i {ssh_key} {ssh_user}@{ssh_ip} 'echo > /dev/null'", 
                            shell=True, check=True)
            except subprocess.CalledProcessError as e:
                log.error(f"    ❌ Failed to connect to worker {worker_name} at {ssh_ip}: {e}")
                continue
            
            # remove docker network
            remote_cmd = f"ssh {remote_str} docker network inspect {sat_vnet}"
            remote_cmd_res = run(remote_cmd)
            if remote_cmd_res.returncode == 0:
                # docker network exist, remove it
                remote_cmd = f"ssh {remote_str} docker network rm {sat_vnet}"
                remote_cmd_res = run(remote_cmd)
                if remote_cmd_res.returncode != 0:
                    log.error(
                        "    ❌ Failed to remove existing remote docker network.\n"
                        f"\t\tCMD: {remote_cmd}\n"
                        f"\t\tSTDOUT: {remote_cmd_res.stdout}\n"
                        f"\t\tSTDERR: {remote_cmd_res.stderr}"
                    )
                else:
                    log.info(f"    ✅ Docker network {sat_vnet} removed successfully.")

            # remove routes to other workers' containers
            for other_name, other_worker in workers.items():
                if other_name == worker_name:
                    continue
                other_ip = other_worker.get("ip", other_name)
                other_cidr = other_worker.get("sat-vnet-cidr", None)
                if not other_cidr:
                    continue

                remote_cmd = f"ssh {remote_str} sudo ip route del {other_cidr} via {other_ip}"
                remote_cmd_res = run(remote_cmd)
                if remote_cmd_res.returncode != 0:
                    log.error(
                         "    ❌ Failed to remove IP route to other worker's containers.\n"
                        f"\t\tCMD: {remote_cmd}\n"
                        f"\t\tSTDOUT: {remote_cmd_res.stdout}\n"
                        f"\t\tSTDERR: {remote_cmd_res.stderr}"
                    )
                else:
                    log.info(f"    ✅ IP route to containers in {other_name} removed successfully.")

            # cleaning iptables rules
            # inserted rule was: -I DOCKER-USER -s super -d super -j ACCEPT
            # delete all matches:
            rule_check = f"sudo iptables -C DOCKER-USER -s {sat_vnet_supercidr} -d {sat_vnet_supercidr} -j ACCEPT"
            remote_cmd = f"ssh {remote_str} {rule_check}"
            remote_cmd_res = run(remote_cmd)
            if remote_cmd_res.returncode == 0:
                # rule exists remove it
                rule_delete = f"sudo iptables -D DOCKER-USER -s {sat_vnet_supercidr} -d {sat_vnet_supercidr} -j ACCEPT"
                remote_cmd = f"ssh {remote_str} {rule_delete}"
                remote_cmd_res = run(remote_cmd)
                if remote_cmd_res.returncode != 0:
                    log.error(
                         "    ❌ Failed to remove DOCKER-USER iptables rule.\n"
                        f"\t\tCMD: {remote_cmd}\n"
                        f"\t\tSTDOUT: {remote_cmd_res.stdout}\n"
                        f"\t\tSTDERR: {remote_cmd_res.stderr}"
                    )
                else:
                    log.info(f"    ✅ DOCKER-USER iptables rule removed successfully.")

            # inserted rule was: -A POSTROUTING -t nat -s {sat_vnet_supercidr} ! -d {sat_vnet_supercidr} -o {default_interface} -j MASQUERADE
            default_interface_cmd = f"ssh {remote_str} ip route show default | awk '/default/ {{print $5}}'"
            default_interface_result = run(default_interface_cmd)
            if default_interface_result.returncode != 0:
                log.error(f"    ❌ Failed to discover default interface on worker {worker_name}, using fallback eth0."
                          f"\t\tCMD: {default_interface_cmd}\n"
                          f"\t\tSTDOUT: {default_interface_result.stdout}\n"
                          f"\t\tSTDERR: {default_interface_result.stderr}")
                default_interface = "eth0"  # fallback
            else:
                default_interface = default_interface_result.stdout.strip()
            rule_check = f"sudo iptables -C POSTROUTING -t nat -s {sat_vnet_supercidr} ! -d {sat_vnet_supercidr} -o {default_interface} -j MASQUERADE"
            remote_cmd = f"ssh {remote_str} {rule_check}"
            remote_cmd_res = run(remote_cmd)
            if remote_cmd_res.returncode == 0:
                # rule exists remove it
                rule_delete = f"sudo iptables -D POSTROUTING -t nat -s {sat_vnet_supercidr} ! -d {sat_vnet_supercidr} -o {default_interface} -j MASQUERADE"
                remote_cmd = f"ssh {remote_str} {rule_delete}"
                remote_cmd_res = run(remote_cmd)
                if remote_cmd_res.returncode != 0:
                    log.error(
                         "    ❌ Failed to remove POSTROUTING NAT iptables rule.\n"
                        f"\t\tCMD: {remote_cmd}\n"
                        f"\t\tSTDOUT: {remote_cmd_res.stdout}\n"
                        f"\t\tSTDERR: {remote_cmd_res.stderr}"
                    )
                else:
                    log.info(f"    ✅ POSTROUTING NAT iptables rule removed successfully.")
            
    # 4) Remove ETCD keys that your script created/overwrote
    etcd_client.delete_prefix("/config/workers/")
    log.info("✅ Removed /config/workers/ prefix")
    log.info("👍 Cleaning completed.")


if __name__ == "__main__":
    main()
