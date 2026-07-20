#!/usr/bin/env python3

import argparse
import concurrent.futures
from datetime import datetime, timedelta, timezone
import json
import logging
import subprocess
import sys
import tempfile
from pathlib import Path

logging.basicConfig(level="INFO", format="[%(levelname)s] %(message)s")
log = logging.getLogger("nsb-ion-prepare")


def run(cmd, *, check=True, capture_output=False):
    result = subprocess.run(
        cmd,
        text=True,
        capture_output=capture_output,
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


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def parse_utc_datetime(value):
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"

    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def format_iso_z(value):
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def repo_root_from_script():
    return Path(__file__).resolve().parents[1]


def default_ion_dir(repo_root):
    return repo_root / "examples/10nodes/ion-dtn"


def scenario_nodes(spec, scenario_dir):
    nodes = set(spec.get("path", []))
    for key in ["default_source", "default_destination"]:
        node = spec.get(key)
        if node:
            nodes.add(node)

    for epoch_path in sorted(Path(scenario_dir).glob("NetSatBench-epoch*.json")):
        epoch = load_json(epoch_path)
        for action in ["links-add", "links-del", "links-update"]:
            for link in epoch.get(action, []):
                nodes.add(link["endpoint1"])
                nodes.add(link["endpoint2"])

    return [name for name in sorted(nodes) if name in spec["nodes"]]


def write_ion_nodes_file(spec, nodes):
    lines = []
    for name in sorted(nodes, key=lambda n: int(spec["nodes"][n]["ion_node"])):
        ion_id = int(spec["nodes"][name]["ion_node"])
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


def list_scenarios(repo_root):
    base = default_scenario_base(repo_root)
    if not base.is_dir():
        return []

    return sorted(path.name for path in base.iterdir() if path.is_dir())


def default_scenario_base(repo_root):
    return repo_root / "examples/10nodes/epochs-ion-dtn-demo"


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
        help="Scenario name under examples/10nodes/epochs-ion-dtn-demo.",
    )
    parser.add_argument(
        "--scenario-dir",
        help="Explicit scenario directory. Overrides --scenario.",
    )
    parser.add_argument(
        "--spec",
        default=str(ion_dir / "path-spec.json"),
        help="ION node spec JSON.",
    )
    parser.add_argument(
        "--epoch-duration",
        type=int,
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
        "--start-delay",
        type=int,
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
    args = parser.parse_args()

    if args.list_scenarios:
        for scenario in list_scenarios(repo_root):
            print(scenario)
        return 0

    spec_path = Path(args.spec).resolve()
    spec = load_json(spec_path)

    scenario_dir = (
        Path(args.scenario_dir).resolve()
        if args.scenario_dir
        else default_scenario_base(repo_root) / args.scenario
    )
    if not scenario_dir.is_dir():
        raise RuntimeError(f"scenario directory not found: {scenario_dir}")

    nodes = args.nodes if args.nodes else scenario_nodes(spec, scenario_dir)
    if not nodes:
        raise RuntimeError("no target nodes selected")
    if args.jobs <= 0:
        raise RuntimeError("--jobs must be positive")

    generator = repo_root / "utils/nsb-ion-generate-configs.py"
    ion_nodes_text = write_ion_nodes_file(spec, nodes)
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
            "--spec",
            str(spec_path),
            "--output",
            str(generated_dir),
            "--epoch-dir",
            str(scenario_dir),
            "--file-pattern",
            args.file_pattern,
            "--epoch-duration",
            str(args.epoch_duration),
        ]
        if absolute_start_time:
            generator_cmd.extend([
                "--absolute-start-time",
                format_iso_z(absolute_start_time),
            ])

        run(generator_cmd, capture_output=True)

        worker_count = min(args.jobs, len(nodes))
        log.info(f"🚀 Preparing ION containers with {worker_count} worker(s)")
        with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(
                    prepare_node,
                    node,
                    generated_dir,
                    ion_nodes_text,
                    args,
                ): node
                for node in nodes
            }
            for future in concurrent.futures.as_completed(futures):
                node = futures[future]
                try:
                    log.info(f"✅ {future.result()}")
                except Exception as exc:
                    raise RuntimeError(f"failed to prepare {node}: {exc}") from exc

    log.info("✅ ION preparation completed. Temporary generated configs were removed from the host.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
