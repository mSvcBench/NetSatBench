#!/usr/bin/env python3

import argparse
import concurrent.futures
from datetime import datetime, timedelta, timezone
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
import math

import etcd3

logging.basicConfig(level="INFO", format="[%(levelname)s] %(message)s")
log = logging.getLogger("nsb-ion-prepare")
DEFAULT_COMMAND_TIMEOUT = 60
NODE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def run(cmd, *, check=True, capture_output=False, timeout=DEFAULT_COMMAND_TIMEOUT):
    result = subprocess.run(
        cmd,
        text=True,
        capture_output=capture_output,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        if capture_output:
            if result.stdout:
                print(result.stdout, file=sys.stdout, end="")
            if result.stderr:
                print(result.stderr, file=sys.stderr, end="")
        raise subprocess.CalledProcessError(
            result.returncode,
            cmd,
            output=result.stdout,
            stderr=result.stderr,
        )

    return result


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def validate_node_name(node):
    if not isinstance(node, str) or not NODE_NAME_PATTERN.fullmatch(node):
        raise RuntimeError(f"invalid container/node name: {node!r}")
    return node


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def parse_utc_datetime(value):
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"

    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    parsed = parsed.astimezone(timezone.utc)
    if parsed.microsecond % 1000:
        raise ValueError("ION contact timestamps support millisecond precision")
    return parsed


def format_iso_z(value):
    value = value.astimezone(timezone.utc)
    rendered = value.strftime("%Y-%m-%dT%H:%M:%S")
    if value.microsecond:
        rendered += f".{value.microsecond // 1000:03d}"
    return rendered + "Z"


def parse_millisecond_seconds(value):
    """Argparse type for non-negative durations with millisecond precision."""
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("expected a duration in seconds") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("duration must be non-negative and finite")
    rounded = round(parsed, 3)
    if not math.isclose(parsed, rounded, abs_tol=1e-9):
        raise argparse.ArgumentTypeError(
            "duration supports at most three decimal places (milliseconds)"
        )
    return rounded


def repo_root_from_script():
    return Path(__file__).resolve().parents[1]


def default_ion_dir(repo_root):
    return repo_root / "examples/10nodes-ion-dtn/ion"


def scenario_nodes(registry, scenario_dir):
    nodes = set(registry.get("path", []))
    for key in ["default_source", "default_destination"]:
        node = registry.get(key)
        if node:
            nodes.add(node)

    for epoch_path in sorted(Path(scenario_dir).glob("NetSatBench-epoch*.json")):
        epoch = load_json(epoch_path)
        for action in ["links-add", "links-del", "links-update"]:
            for link in epoch.get(action, []):
                nodes.add(link["endpoint1"])
                nodes.add(link["endpoint2"])

    return [name for name in sorted(nodes) if name in registry["nodes"]]


def write_ion_nodes_file(registry, nodes):
    lines = []
    for name in sorted(nodes, key=lambda n: int(registry["nodes"][n]["ion_node_number"])):
        ion_id = int(registry["nodes"][name]["ion_node_number"])
        wm_key = 90000 + ion_id
        lines.append(f"{ion_id} {wm_key} ion{ion_id} /ion/{name} -1")

    return "\n".join(lines) + "\n"


def ensure_container_exists(node):
    result = run(
        ["docker", "inspect", "--format", "{{.Name}}", node],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"container is not available: {node}")


def local_container_exists(node):
    result = run(
        ["docker", "inspect", "--format", "{{.Name}}", node],
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


def connect_etcd(args):
    if args.etcd_user and args.etcd_password:
        client = etcd3.client(
            host=args.etcd_host,
            port=args.etcd_port,
            user=args.etcd_user,
            password=args.etcd_password,
            ca_cert=args.etcd_ca_cert,
        )
    else:
        client = etcd3.client(host=args.etcd_host, port=args.etcd_port)
    client.status()
    return client


def load_etcd_json(client, key):
    value, _ = client.get(key)
    if not value:
        raise RuntimeError(f"Etcd key is missing: {key}")
    return json.loads(value.decode("utf-8"))


def resolve_remote_workers(client, nodes):
    targets = {}
    for node in nodes:
        node_config = load_etcd_json(client, f"/config/nodes/{node}")
        worker_name = node_config.get("worker")
        if not worker_name:
            raise RuntimeError(f"node has no assigned worker in Etcd: {node}")
        worker = load_etcd_json(client, f"/config/workers/{worker_name}")
        worker_host = worker.get("ip", worker_name)
        if not worker_host:
            raise RuntimeError(f"worker has no reachable IP: {worker_name}")
        targets[node] = {
            "name": worker_name,
            "host": worker_host,
            "user": worker.get("ssh-user", "ubuntu"),
            "key": os.path.expanduser(worker.get("ssh-key", "~/.ssh/id_rsa")),
        }
    return targets


def run_worker_ssh(worker, remote_command, timeout, check=True):
    return run(
        [
            "ssh",
            "-i",
            worker["key"],
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"ConnectTimeout={timeout}",
            f"{worker['user']}@{worker['host']}",
            f"sh -lc {shlex.quote(remote_command)}",
        ],
        check=check,
        capture_output=True,
        timeout=timeout + 30,
    )


def copy_to_worker(local_path, worker, remote_path, timeout):
    return run(
        [
            "scp",
            "-i",
            worker["key"],
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"ConnectTimeout={timeout}",
            str(local_path),
            f"{worker['user']}@{worker['host']}:{remote_path}",
        ],
        capture_output=True,
        timeout=timeout + 120,
    )


def build_node_archive(node, generated_dir, ion_nodes_text, archive_path):
    node_dir = generated_dir / node
    if not node_dir.is_dir():
        raise RuntimeError(f"generated config is missing node directory: {node_dir}")

    ion_nodes_path = generated_dir / "ion_nodes"
    ion_nodes_path.write_text(ion_nodes_text, encoding="utf-8")
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(generated_dir / "global.ionrc", arcname="global.ionrc")
        archive.add(ion_nodes_path, arcname="ion_nodes")
        archive.add(node_dir, arcname=node)


def reset_container_ion_dir(node):
    script = (
        "import os, shutil\n"
        "shutil.rmtree('/ion', ignore_errors=True)\n"
        "os.makedirs('/ion', exist_ok=True)\n"
    )
    run(["docker", "exec", node, "python3", "-c", script])


def copy_node_config(node, generated_dir, ion_nodes_text):
    node_dir = generated_dir / node
    if not node_dir.is_dir():
        raise RuntimeError(f"generated config is missing node directory: {node_dir}")

    run(["docker", "exec", node, "mkdir", "-p", f"/ion/{node}"])
    run(["docker", "cp", str(generated_dir / "global.ionrc"), f"{node}:/ion/global.ionrc"])
    run(["docker", "cp", f"{node_dir}/.", f"{node}:/ion/{node}/"])

    writer = (
        "from pathlib import Path\n"
        "Path('/ion/ion_nodes').write_text(" + repr(ion_nodes_text) + ", encoding='utf-8')\n"
    )
    run(["docker", "exec", node, "python3", "-c", writer])


def stop_ion(node):
    cmd = (
        "export ION_NODE_LIST_DIR=/ion\n"
        f"if [ -d /ion/{node} ]; then cd /ion/{node}; fi\n"
        "ionstop >/dev/null 2>&1 || true\n"
        "ionexit >/dev/null 2>&1 || true\n"
        "killm >/dev/null 2>&1 || true\n"
        "for proc in bprecvfile bpsendfile rfxclock bpclm bpclock bptransit "
        "ipnfw ipnadminep udpcli udpclo cpsd ionwarn; do "
        "pkill -x \"$proc\" >/dev/null 2>&1 || true; "
        "done\n"
        "sleep 1\n"
    )
    run(["docker", "exec", node, "sh", "-lc", cmd], check=False)


def start_ion(node):
    cmd = (
        "export ION_NODE_LIST_DIR=/ion\n"
        f"cd /ion/{node}\n"
        "ionadmin node.ionrc > ion.log 2>&1\n"
        "ionsecadmin node.ionsecrc >> ion.log 2>&1\n"
        "bpadmin node.bprc >> ion.log 2>&1\n"
    )
    run(["docker", "exec", node, "sh", "-lc", cmd])


def prepare_node(node, generated_dir, ion_nodes_text, args):
    if args.dry_run:
        return f"Dry run: would prepare /ion for {node}"

    ensure_container_exists(node)
    if not args.no_stop:
        stop_ion(node)
    reset_container_ion_dir(node)
    copy_node_config(node, generated_dir, ion_nodes_text)
    if not args.no_start:
        start_ion(node)

    return f"Prepared {node}"


def prepare_remote_node(node, worker, archive_path, args):
    if args.dry_run:
        return (
            f"Dry run: would prepare /ion for {node} on "
            f"{worker['name']} ({worker['host']})"
        )

    remote_archive = f"/tmp/nsb-ion-prepare-{node}.tar.gz"
    container_archive = f"/tmp/nsb-ion-prepare-{node}.tar.gz"
    try:
        copy_to_worker(archive_path, worker, remote_archive, args.ssh_timeout)

        stop_commands = ""
        if not args.no_stop:
            stop_commands = (
                "export ION_NODE_LIST_DIR=/ion; "
                f"if [ -d /ion/{node} ]; then cd /ion/{node}; fi; "
                "ionstop >/dev/null 2>&1 || true; "
                "ionexit >/dev/null 2>&1 || true; "
                "killm >/dev/null 2>&1 || true; "
                "for proc in bprecvfile bpsendfile rfxclock bpclm bpclock bptransit "
                "ipnfw ipnadminep udpcli udpclo cpsd ionwarn; do "
                "pkill -x \"$proc\" >/dev/null 2>&1 || true; done; sleep 1; "
            )

        start_commands = ""
        if not args.no_start:
            start_commands = (
                "export ION_NODE_LIST_DIR=/ion; "
                f"cd /ion/{node}; "
                "ionadmin node.ionrc > ion.log 2>&1; "
                "ionsecadmin node.ionsecrc >> ion.log 2>&1; "
                "bpadmin node.bprc >> ion.log 2>&1; "
            )

        remote_command = (
            f"docker inspect {node} >/dev/null 2>&1 && "
            f"docker exec {node} sh -lc '{stop_commands}' && "
            f"docker cp {remote_archive} {node}:{container_archive} && "
            f"docker exec {node} python3 -c \"import os,shutil,tarfile; "
            "shutil.rmtree('/ion',ignore_errors=True); os.makedirs('/ion'); "
            f"tarfile.open('{container_archive}','r:gz').extractall('/ion'); "
            f"os.remove('{container_archive}')\" && "
            f"docker exec {node} sh -lc '{start_commands}'"
        )

        run_worker_ssh(worker, remote_command, args.ssh_timeout)
    finally:
        run_worker_ssh(
            worker,
            f"rm -f {remote_archive}",
            args.ssh_timeout,
            check=False,
        )
    return f"Prepared {node} on {worker['name']} ({worker['host']})"


def stop_remote_ion(node, worker, args):
    script = (
        "export ION_NODE_LIST_DIR=/ion; "
        f"if [ -d /ion/{node} ]; then cd /ion/{node}; fi; "
        "ionstop >/dev/null 2>&1 || true; "
        "ionexit >/dev/null 2>&1 || true; "
        "killm >/dev/null 2>&1 || true"
    )
    run_worker_ssh(
        worker,
        f"docker exec {node} sh -lc {shlex.quote(script)}",
        args.ssh_timeout,
        check=False,
    )


def rollback_preparation(nodes, local_nodes, remote_workers, args):
    """Leave every selected node safely stopped after a partial preparation."""
    errors = []
    for node in nodes:
        try:
            if node in local_nodes:
                stop_ion(node)
            else:
                stop_remote_ion(node, remote_workers[node], args)
        except Exception as error:
            errors.append(f"{node}: {error}")
    return errors


def list_scenarios(repo_root):
    base = default_scenario_base(repo_root)
    if not base.is_dir():
        return []

    return sorted(path.name for path in base.iterdir() if path.is_dir())


def default_scenario_base(repo_root):
    return repo_root / "examples/10nodes-ion-dtn/epochs"


def main():
    repo_root = repo_root_from_script()
    ion_dir = default_ion_dir(repo_root)

    parser = argparse.ArgumentParser(
        description=(
            "Generate ION configs in a temporary directory, copy them into "
            "NetSatBench containers, and optionally restart ION."
        )
    )
    parser.add_argument(
        "--scenario",
        default="02-cloudy-ground",
        help="Scenario name under examples/10nodes-ion-dtn/epochs.",
    )
    parser.add_argument(
        "--scenario-dir",
        help="Explicit scenario directory. Overrides --scenario.",
    )
    parser.add_argument(
        "--registry",
        "--spec",
        dest="registry_path",
        default=str(ion_dir / "ion-node-registry.json"),
        help="ION node registry JSON.",
    )
    parser.add_argument(
        "--epoch-duration",
        type=parse_millisecond_seconds,
        default=45,
        help=(
            "Seconds used to close contacts that remain active after the last "
            "epoch. Contact start times come from epoch timestamps."
        ),
    )
    parser.add_argument(
        "--absolute-start-time",
        help=(
            "UTC experiment T0 used for absolute ION contact times, for example "
            "2026-07-20T10:30:00Z."
        ),
    )
    parser.add_argument(
        "--contact-start-guard",
        type=parse_millisecond_seconds,
        default=4,
        help=(
            "Delay each ION contact after its physical links-add event by this "
            "many seconds (canonical default: 4)."
        ),
    )
    parser.add_argument(
        "--contact-end-guard",
        type=parse_millisecond_seconds,
        default=1,
        help=(
            "Close each ION contact this many seconds before physical link "
            "removal or scenario end (canonical default: 1)."
        ),
    )
    parser.add_argument(
        "--start-delay",
        type=parse_millisecond_seconds,
        help=(
            "Compute absolute experiment T0 as current UTC time plus this many "
            "seconds. Ignored if --absolute-start-time is set."
        ),
    )
    parser.add_argument(
        "--file-pattern",
        default="NetSatBench-epoch*.json",
        help="Epoch file glob.",
    )
    parser.add_argument(
        "--nodes",
        nargs="+",
        help="Containers to prepare. Defaults to nodes referenced by the scenario.",
    )
    parser.add_argument(
        "--no-start",
        action="store_true",
        help="Only copy configs; do not start ION.",
    )
    parser.add_argument(
        "--no-stop",
        action="store_true",
        help="Do not call killm before copying and starting.",
    )
    parser.add_argument(
        "--list-scenarios",
        action="store_true",
        help="List available scenarios and exit.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate configs in a temporary directory and print planned container actions only.",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=8,
        help="Maximum containers to prepare in parallel. Use 1 for sequential preparation.",
    )
    parser.add_argument(
        "--transport",
        choices=("auto", "local", "ssh"),
        default="auto",
        help=(
            "Container transport: auto uses local Docker when available and "
            "otherwise resolves the node worker from Etcd; local requires all "
            "containers on this host; ssh always uses the assigned worker."
        ),
    )
    parser.add_argument(
        "--ssh-timeout",
        type=int,
        default=30,
        help="SSH/SCP connection timeout in seconds (default: 30).",
    )
    parser.add_argument("--etcd-host", default=os.getenv("ETCD_HOST", "127.0.0.1"))
    parser.add_argument(
        "--etcd-port", type=int, default=int(os.getenv("ETCD_PORT", "2379"))
    )
    parser.add_argument("--etcd-user", default=os.getenv("ETCD_USER"))
    parser.add_argument("--etcd-password", default=os.getenv("ETCD_PASSWORD"))
    parser.add_argument("--etcd-ca-cert", default=os.getenv("ETCD_CA_CERT"))
    args = parser.parse_args()

    if args.list_scenarios:
        for scenario in list_scenarios(repo_root):
            print(scenario)
        return 0

    registry_path = Path(args.registry_path).resolve()
    registry = load_json(registry_path)

    scenario_dir = (
        Path(args.scenario_dir).resolve()
        if args.scenario_dir
        else default_scenario_base(repo_root) / args.scenario
    )
    if not scenario_dir.is_dir():
        raise RuntimeError(f"scenario directory not found: {scenario_dir}")

    nodes = args.nodes if args.nodes else scenario_nodes(registry, scenario_dir)
    if not nodes:
        raise RuntimeError("no target nodes selected")
    nodes = [validate_node_name(node) for node in nodes]
    if len(nodes) != len(set(nodes)):
        raise RuntimeError("target node list contains duplicates")
    if args.jobs <= 0:
        raise RuntimeError("--jobs must be positive")
    if args.ssh_timeout <= 0:
        raise RuntimeError("--ssh-timeout must be positive")
    if args.contact_start_guard < 0 or args.contact_end_guard < 0:
        raise RuntimeError("contact guards must be non-negative")
    if args.contact_start_guard + args.contact_end_guard >= args.epoch_duration:
        raise RuntimeError(
            "contact guards must leave a positive window within one epoch"
        )
    if args.contact_start_guard == 0 and args.contact_end_guard == 0:
        log.warning(
            "ION contacts share physical link boundaries; use contact guards "
            "when link setup or teardown is not instantaneous"
        )

    generator = repo_root / "utils/nsb-ion-generate-configs.py"
    ion_nodes_text = write_ion_nodes_file(registry, nodes)
    absolute_start_time = None
    if args.absolute_start_time:
        absolute_start_time = parse_utc_datetime(args.absolute_start_time)
    elif args.start_delay is not None:
        if args.start_delay <= 0:
            raise RuntimeError("--start-delay must be positive")
        absolute_start_time = utc_now() + timedelta(seconds=args.start_delay)

    log.info(f"🛰️ ION scenario: {scenario_dir}")
    log.info(f"🧩 ION nodes: {' '.join(nodes)}")
    if absolute_start_time:
        log.info(f"🕒 Absolute experiment T0: {format_iso_z(absolute_start_time)}")

    with tempfile.TemporaryDirectory(prefix="nsb-ion-generated-") as tmp:
        generated_dir = Path(tmp) / "generated"
        generator_cmd = [
            sys.executable,
            str(generator),
            "--registry",
            str(registry_path),
            "--output",
            str(generated_dir),
            "--epoch-dir",
            str(scenario_dir),
            "--file-pattern",
            args.file_pattern,
            "--epoch-duration",
            str(args.epoch_duration),
            "--contact-start-guard",
            str(args.contact_start_guard),
            "--contact-end-guard",
            str(args.contact_end_guard),
        ]
        if absolute_start_time:
            generator_cmd.extend([
                "--absolute-start-time",
                format_iso_z(absolute_start_time),
            ])

        run(generator_cmd, capture_output=True)

        local_nodes = {
            node for node in nodes
            if args.transport != "ssh" and local_container_exists(node)
        }
        if args.transport == "local" and local_nodes != set(nodes):
            missing = sorted(set(nodes) - local_nodes)
            raise RuntimeError(
                "containers are not available on the local Docker daemon: "
                + ", ".join(missing)
            )

        remote_nodes = set(nodes) - local_nodes
        remote_workers = {}
        archives = {}
        if remote_nodes:
            etcd_client = connect_etcd(args)
            remote_workers = resolve_remote_workers(etcd_client, remote_nodes)
            archive_dir = Path(tmp) / "archives"
            archive_dir.mkdir()
            for node in remote_nodes:
                archive_path = archive_dir / f"{node}.tar.gz"
                build_node_archive(
                    node,
                    generated_dir,
                    ion_nodes_text,
                    archive_path,
                )
                archives[node] = archive_path

        if local_nodes:
            log.info("📍 Local Docker nodes: %s", " ".join(sorted(local_nodes)))
        if remote_nodes:
            log.info(
                "🌐 SSH worker nodes: %s",
                " ".join(
                    f"{node}@{remote_workers[node]['name']}"
                    for node in sorted(remote_nodes)
                ),
            )

        worker_count = min(args.jobs, len(nodes))
        log.info(f"🚀 Preparing ION containers with {worker_count} worker(s)")
        failures = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {}
            for node in nodes:
                if node in local_nodes:
                    future = executor.submit(
                        prepare_node,
                        node,
                        generated_dir,
                        ion_nodes_text,
                        args,
                    )
                else:
                    future = executor.submit(
                        prepare_remote_node,
                        node,
                        remote_workers[node],
                        archives[node],
                        args,
                    )
                futures[future] = node
            for future in concurrent.futures.as_completed(futures):
                node = futures[future]
                try:
                    log.info(f"✅ {future.result()}")
                except Exception as exc:
                    failures[node] = exc

        if failures:
            cleanup_errors = rollback_preparation(
                nodes,
                local_nodes,
                remote_workers,
                args,
            )
            details = "; ".join(
                f"{node}: {error}" for node, error in sorted(failures.items())
            )
            if cleanup_errors:
                details += "; rollback errors: " + "; ".join(cleanup_errors)
            raise RuntimeError(
                "ION preparation failed; all selected nodes were stopped: " + details
            )

    log.info("✅ ION preparation completed. Temporary generated configs were removed from the host.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
