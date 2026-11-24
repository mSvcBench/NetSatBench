#!/usr/bin/env python3
"""Periodically scrape /debug/state of all the llmd_emu workers of a deployment.

The workers are read from the llmd sat config yaml (e.g. llmd-sat-config.yaml), the same file used by
deploy/llmd_emu_sat_deploy.py:
    workers: [ {node: sat13, routers: [sat1, ...] [, port: 9000]} ]
As in the deploy script, a worker registered at k routers runs one process per router, on ports port, port+1, ...
(router routers[i] <-> port + i), and is reached at http://<node>:<port>.

Every --interval seconds all the worker processes are queried in parallel and one JSON line is appended to --output:
    {"time": <unix s>, "elapsed_s": <s from start>,
     "workers": [ {<worker /debug/state>, "node": "sat13", "port": 9000, "url": "http://sat13:9000",
                   "router": "sat1",            # router this process is registered at
                   "routers": ["sat1", ...],    # all the routers of the worker (from the config)
                   "ok": true} , ...]}
An unreachable worker gives {"node", "port", "url", "router", "routers", "ok": false, "error": "..."}.

usage: scraper.py --sat-config llmd-sat-config.yaml [--interval 1] [--output scrape.jsonl] [--duration 600]
Stop with Ctrl-C.
"""
import argparse
import concurrent.futures
import json
import sys
import time
import urllib.request
from typing import Dict, List

import yaml


def load_workers(sat_config: str) -> List[Dict]:
    """One entry per worker process: {node, port, url, router, routers}."""
    with open(sat_config, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    out = []
    for w in cfg.get("workers", []) or []:
        node, base = w["node"], int(w.get("port", 9000))
        routers = list(w.get("routers") or [])
        for k, r in enumerate(routers or [None]):
            port = base + k
            out.append({"node": node, "port": port, "url": f"http://{node}:{port}", "router": r, "routers": routers})
    return out


def scrape(w: Dict, timeout: float) -> Dict:
    try:
        with urllib.request.urlopen(f"{w['url']}/debug/state", timeout=timeout) as r:
            state = json.loads(r.read().decode())
        return {**state, **w, "ok": True}
    except Exception as e:  # noqa: BLE001  (unreachable, timeout, bad JSON: recorded, scraping goes on)
        return {**w, "ok": False, "error": f"{type(e).__name__}: {e}"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sat-config", required=True, help="llmd sat config yaml (e.g., llmd-sat-config.yaml)")
    ap.add_argument("--interval", type=float, default=1.0, help="seconds between two scrapes (default 1)")
    ap.add_argument("--timeout", type=float, default=2.0, help="HTTP timeout per worker, s (default 2)")
    ap.add_argument("--duration", type=float, default=0.0, help="stop after this many seconds; 0 = until Ctrl-C (default 0)")
    ap.add_argument("--output", default="scrape.json", help="output file, one JSON line per scrape (default scrape.json)")
    ap.add_argument("--quiet", action="store_true", help="do not print a summary line per scrape")
    a = ap.parse_args()

    workers = load_workers(a.sat_config)
    if not workers:
        print(f"no workers in {a.sat_config}", file=sys.stderr)
        return 1
    print(f"{len(workers)} worker process(es); scraping every {a.interval}s into {a.output}", flush=True)

    t0 = time.time()
    with open(a.output, "w", encoding="utf-8") as out, \
            concurrent.futures.ThreadPoolExecutor(max_workers=min(64, len(workers))) as ex:
        try:
            while True:
                tick = time.time()
                states = list(ex.map(lambda w: scrape(w, a.timeout), workers))
                out.write(json.dumps({"time": tick, "elapsed_s": round(tick - t0, 3), "workers": states}) + "\n")
                out.flush()
                if not a.quiet:
                    ok = [s for s in states if s["ok"]]
                    print(f"t={tick - t0:8.1f}s  ok {len(ok)}/{len(states)}  "
                          f"inflight {sum(s.get('inflight', 0) for s in ok)}  "
                          f"completed {sum(s.get('completed', 0) for s in ok)}", flush=True)
                if a.duration > 0 and time.time() - t0 >= a.duration:
                    break
                time.sleep(max(0.0, a.interval - (time.time() - tick)))   # fixed period, scrape time included
        except KeyboardInterrupt:
            pass
    print(f"done, {a.output} written", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
