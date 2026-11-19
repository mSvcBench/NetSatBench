#!/usr/bin/env python3
"""Configurable llmd_emu deployment (run from the NetSatBench repo root).

usage: test/satLLMD/deploy/llmd_emu_sat_deploy.py -c <cluster.yaml> [options]

cluster.yaml (YAML; the same structure is shown as JSON):
  {
    "routers":  [ {"node": "sat1" [, "port": 8100]} ],
    "gateways": [ {"node": "sat2",  "routers": ["sat1"] [, "port": 8000]} ],
    "workers":  [ {"node": "sat13", "routers": ["sat1"] [, "port": 9000]} ]
  }
"routers" of gateways/workers are node names of entries of the "routers" list.
 - gateway: only the first router of the list is used.
 - worker: it registers at every router of the list. llmd_emu workers accept a single --router-url, so one
   worker process is started per router (screen session LLMD_WORKER for the first, LLMD_WORKER_<k> for the
   others, on ports port, port+1, ...).
"""
import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import yaml
from typing import Dict, List, Optional

NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
CONTAINER_VOLUME_DIR = "/app/shared/satLLMD"
DEPLOY_DIR = "test/satLLMD/deploy"     # tarballs, cluster configs
SOURCE_DIR = "test/satLLMD"            # llmd_emu and srv6 sources


# ----------------------------
#   CONFIG
# ----------------------------
class ConfigError(Exception):
    pass


def load_config(path: str) -> Dict[str, List[dict]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"cannot read {path}: {e}")

    def items(key: str) -> List[dict]:
        v = cfg.get(key, [])
        if not isinstance(v, list):
            raise ConfigError(f"'{key}' must be a list")
        for o in v:
            if not isinstance(o, dict) or not isinstance(o.get("node"), str) or not NAME_RE.match(o["node"]):
                raise ConfigError(f"every entry of '{key}' needs a valid 'node' name: {o}")
        return v

    agents = {k: items(k) for k in ("routers", "gateways", "workers")}
    router_ports: Dict[str, int] = {}
    for r in agents["routers"]:
        if r["node"] in router_ports:
            raise ConfigError(f"router '{r['node']}' defined twice")
        router_ports[r["node"]] = int(r.get("port", 8100))
    for kind, key in (("gateway", "gateways"), ("worker", "workers")):
        for o in agents[key]:
            rs = o.get("routers")   # routers of gateway and workers
            if not isinstance(rs, list) or not rs:
                raise ConfigError(f"{kind} '{o['node']}' needs a non-empty 'routers' list")
            for r in rs:
                if r not in router_ports:
                    raise ConfigError(f"{kind} '{o['node']}' references unknown router '{r}'")
    for g in agents["gateways"]:
        if len(g["routers"]) > 1:
            print(f"warning: gateway '{g['node']}' lists {len(g['routers'])} routers, only '{g['routers'][0]}' is used",
                  file=sys.stderr)
    agents["_router_ports"] = router_ports  # type: ignore[assignment]
    return agents


# ----------------------------
#   EXECUTION
# ----------------------------
class Runner:
    def __init__(self, dry: bool, max_jobs: int):
        self.dry, self.max_jobs = dry, max_jobs
        self.failures: List[str] = []

    def run(self, cmd: List[str], cwd: Optional[str] = None) -> bool:
        if self.dry:
            print("+ " + " ".join(cmd), flush=True)
            return True
        r = subprocess.run(cmd, cwd=cwd)
        if r.returncode != 0:
            self.failures.append(" ".join(cmd[:4]))
        return r.returncode == 0

    def parallel(self, jobs: List[List[str]]) -> None:
        """Run commands in parallel, at most max_jobs at a time; returns when all are done."""
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_jobs) as ex:
            list(ex.map(self.run, jobs))


def screen_cmd(session: str, command: str, prefix: str = "") -> str:
    """Remote command: (re)start a detached screen session and type `command` into it.
    `screen -d -m` returns before the new session accepts input, and the old session may still be dying after
    `quit`: wait until the old session is gone, then retry `stuff` until the new session answers."""
    return (f"{prefix}screen -S {session} -X quit >/dev/null 2>&1; "
            f"for i in $(seq 50); do screen -ls 2>/dev/null | grep -q '[0-9]\\.{session}[[:space:]]' || break; sleep 0.2; done; "
            f"/usr/bin/screen -S {session} -s /bin/bash -t win0 -A -d -m; "
            f"for i in $(seq 50); do screen -S {session} -p win0 -X stuff $'{command}\\n' >/dev/null 2>&1 && break; sleep 0.2; done")


def llmd_exec(node: str, session: str, command: str) -> List[str]:
    remote = screen_cmd(session, f"source .venv/bin/activate; {command}", prefix=f"cd {CONTAINER_VOLUME_DIR}/llmd_emu; ")
    return ["nsb.py", "exec", node, remote]


# ----------------------------
#   MAIN
# ----------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", required=True, help="cluster description (see llmd-sat-config.yaml)")
    ap.add_argument("--skip-package", action="store_true", help="do not build/copy the software package")
    ap.add_argument("--skip-srv6", action="store_true", help="do not (re)start the srv6 learning scripts on satellites and users")
    ap.add_argument("--dry-run", action="store_true", help="print the commands instead of running them")
    ap.add_argument("--max-jobs", type=int, default=int(os.getenv("MAX_JOBS", 8)), help="max parallel nsb.py calls (default 8)")
    ap.add_argument("--volume-host", default="nsb-1", help="host holding the volume shared with the nodes (default nsb-1)")
    ap.add_argument("--volume-dir", default="~/shared/satLLMD",
                    help="directory on the volume host where the package is extracted (default ~/shared/satLLMD)")
    a = ap.parse_args()

    try:
        cfg = load_config(a.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1
    routers, gateways, workers = cfg["routers"], cfg["gateways"], cfg["workers"]
    rports: Dict[str, int] = cfg["_router_ports"]  # type: ignore[assignment]
    n_w = sum(len(w["routers"]) for w in workers)
    print(f"Cluster: {len(routers)} router(s), {len(gateways)} gateway(s), {n_w} worker process(es)")

    r = Runner(a.dry_run, max(1, a.max_jobs))

    # ----- software package -----
    if not a.skip_package:
        if not a.dry_run:
            try:
                subprocess.run(["git", "pull"], cwd=os.path.join(SOURCE_DIR, "llmd_emu"), check=True)
                subprocess.run(["tar", "--no-xattrs", "-czf", os.path.join(DEPLOY_DIR, "llmd_emu.tar.gz"),
                                "-C", SOURCE_DIR, "llmd_emu"], check=True)
                subprocess.run(["tar", "--no-xattrs", "-cf", os.path.abspath(os.path.join(DEPLOY_DIR, "software_package.tar")),
                                "-C", os.path.abspath(DEPLOY_DIR), "llmd_emu.tar.gz", "venv.tar.gz",
                                "-C", os.path.abspath(SOURCE_DIR), "srv6"], check=True)
            except (subprocess.CalledProcessError, OSError) as e:
                print(f"package preparation failed: {e}", file=sys.stderr)
                return 1
        else:
            print("+ git pull / tar llmd_emu.tar.gz / tar software_package.tar (in test/satLLMD/deploy)")
        r.run(["scp", os.path.join(DEPLOY_DIR, "software_package.tar"), f"{a.volume_host}:{a.volume_dir}/software_package.tar"])
        r.run(["ssh", a.volume_host,
               f"cd {a.volume_dir}; sudo rm -rf llmd_emu srv6; tar -xf software_package.tar; rm software_package.tar; "
               "tar -xf llmd_emu.tar.gz; rm llmd_emu.tar.gz; tar -xf venv.tar.gz -C llmd_emu; rm venv.tar.gz"])

    # ----- copy the sat and emu config to the volume (always, also with --skip-package) -----
    r.run(["scp", a.config, f"{a.volume_host}:{a.volume_dir}/llmd-sat-config.yaml"])
    r.run(["scp", a.config, f"{a.volume_host}:{a.volume_dir}/llmd-emu-config.yaml"])

    # copy benchmark folder to volume host (always, also with --skip-package) -----
    r.run(["scp", "-r", os.path.join(SOURCE_DIR, "benchmark"), f"{a.volume_host}:{a.volume_dir}/benchmark"])
    
    # ----- srv6 -----
    if not a.skip_srv6:
        r.run(["nsb.py", "exectype", "--node-type", "satellite", "-t", str(a.max_jobs),
               screen_cmd("SAT_SRV6_LEARN", f"python3 {CONTAINER_VOLUME_DIR}/srv6/sat_srv6_learn.py")])
        r.run(["nsb.py", "exectype", "--node-type", "user", "-t", str(a.max_jobs),
               screen_cmd("USR_SRV6_LEARN", f"python3 {CONTAINER_VOLUME_DIR}/srv6/usr_srv6_learn.py")])

    # ----- routers -----
    jobs = []
    for o in routers:
        print(f"Starting router on {o['node']} (port {rports[o['node']]})")
        jobs.append(llmd_exec(o["node"], "LLMD_ROUTER",
                              f"python3 -m llmd_emu router -c {CONTAINER_VOLUME_DIR}/llmd-sat-config.yaml --port {rports[o['node']]} --name {o['node']}"))
    r.parallel(jobs)

    # ----- gateways (first router of the list only) -----
    jobs = []
    for o in gateways:
        rn = o["routers"][0]
        port = int(o.get("port", 8000))
        print(f"Starting gateway on {o['node']} (port {port}) -> router {rn}:{rports[rn]}")
        jobs.append(llmd_exec(o["node"], "LLMD_GATEWAY",
                              f"python3 -m llmd_emu gateway -c {CONTAINER_VOLUME_DIR}/llmd-sat-config.yaml --router-url http://{rn}:{rports[rn]} --port {port} "
                              f"--name {o['node']}"))
    r.parallel(jobs)

    # ----- workers (one process per router in the list) -----
    jobs = []
    for o in workers:
        base = int(o.get("port", 9000))
        for k, rn in enumerate(o["routers"]):
            port = base + k
            session = "LLMD_WORKER" if k == 0 else f"LLMD_WORKER_{k}"
            print(f"Starting worker on {o['node']} (port {port}) -> router {rn}:{rports[rn]}")
            jobs.append(llmd_exec(o["node"], session,
                                  f"python3 -m llmd_emu worker -c {CONTAINER_VOLUME_DIR}/llmd-sat-config.yaml --router-url http://{rn}:{rports[rn]} --port {port} "
                                  f"--name {o['node']} --url http://{o['node']}:{port}"))
    r.parallel(jobs)

    if r.failures:
        print(f"{len(r.failures)} command(s) failed:", file=sys.stderr)
        for f in r.failures:
            print(f"  {f} ...", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
