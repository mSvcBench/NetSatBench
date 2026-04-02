#!/usr/bin/env python3

import sys
import argparse
import subprocess
from pathlib import Path

# Mapping between CLI subcommands and script execution settings
COMMANDS = {
    "system-init-docker": {"script": "control/system-init-docker.py"},
    "system-clean-docker": {"script": "control/system-clean-docker.py"},
    "init": {"script": "control/nsb-init.py"},
    "deploy": {"script": "control/nsb-deploy.py"},
    "run": {"script": "control/nsb-run.py"},
    "node-restart": {"script": "control/nsb-node-restart.py"},
    "rm": {"script": "control/nsb-rm.py"},
    "stats": {"script": "utils/nsb-stats.py"},
    "exec": {"script": "utils/nsb-exec.py"},
    "exectype": {"script": "utils/nsb-exectype.py"},
    "cp": {"script": "utils/nsb-cp.py"},
    "cptype": {"script": "utils/nsb-cptype.py"},
    "reset": {"script": "control/nsb-reset.py"},
    "inspect": {"script": "utils/nsb-inspect.py"},
    "status": {"script": "utils/nsb-status.py"},
    "run-inject": {"script": "utils/nsb-run-inject.py"},
    "starperf-generate": {
        "script": "generators/StarPerf_Simulator/kits/NetSatBench/NetSatBenchGenerate.py",
        "cwd": "generators/StarPerf_Simulator",
    },
    "starperf-visualize": {
        "script": "generators/StarPerf_Simulator/kits/NetSatBench/NetSatBenchVisualizer.py",
        "cwd": "generators/StarPerf_Simulator",
    },
    "starperf-export": {
        "script": "generators/StarPerf_Simulator/kits/NetSatBench/NetSatBenchExport.py",
        "cwd": "generators/StarPerf_Simulator",
    },
    "starperf-matlab-visualize": {
        "script": "utils/nsb-starperf-matlab-visualize.py",
        "cwd": "generators/StarPerf_Simulator",
    },
}


def main():
    parser = argparse.ArgumentParser(
        prog="nsb",
        description="NetSatBench CLI"
    )

    parser.add_argument(
        "command",
        help=f"Command to execute ({', '.join(COMMANDS.keys())})"
    )

    parser.add_argument(
        "args",
        nargs=argparse.REMAINDER,
        help="Arguments passed to the subcommand"
    )

    parsed = parser.parse_args()

    command = parsed.command

    if command not in COMMANDS:
        print(f"Unknown command: {command}")
        print(f"Available commands: {', '.join(COMMANDS.keys())}")
        sys.exit(1)

    command_config = COMMANDS[command]
    repo_root = Path(__file__).resolve().parent
    script_path = repo_root / command_config["script"]
    command_cwd = repo_root / command_config.get("cwd", ".")

    if not script_path.exists():
        print(f"Script not found: {script_path}")
        sys.exit(1)

    if not command_cwd.exists():
        print(f"Working directory not found: {command_cwd}")
        sys.exit(1)

    # Execute the subcommand script
    cmd = [sys.executable, str(script_path)] + parsed.args
    result = subprocess.run(cmd, cwd=command_cwd)

    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
