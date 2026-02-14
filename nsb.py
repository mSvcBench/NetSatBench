#!/usr/bin/env python3
#!/usr/bin/env python3

import sys
import argparse
import subprocess
from pathlib import Path

# Mapping between CLI subcommands and script filenames
COMMANDS = {
    "init": "control/nsb-init.py",
    "deploy": "control/nsb-deploy.py",
    "run": "control/nsb-run.py",
    "rm": "control/nsb-rm.py",
    "stats": "utils/nsb-stats.py",
    "exec": "utils/nsb-exec.py",
    "cp": "utils/nsb-cp.py",
    "unlink": "utils/nsb-unlink.py",
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

    script_name = COMMANDS[command]
    script_path = Path(__file__).parent / script_name

    if not script_path.exists():
        print(f"Script not found: {script_path}")
        sys.exit(1)

    # Execute the subcommand script
    cmd = [sys.executable, str(script_path)] + parsed.args
    result = subprocess.run(cmd)

    sys.exit(result.returncode)


if __name__ == "__main__":
    main()


