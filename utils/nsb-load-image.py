#!/usr/bin/env python3

"""Distribute a staged Docker image to the selected NetSatBench workers."""

import argparse
import concurrent.futures
import gzip
import json
import logging
import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple

import etcd3

logging.basicConfig(level="INFO", format="[%(levelname)s] %(message)s")
log = logging.getLogger("nsb-load-image")


# ==========================================
# COMMAND HELPERS
# ==========================================
def run(cmd, *, check=True, stdin=None, stdout=None, timeout=None):
    result = subprocess.run(
        cmd,
        stdin=stdin,
        stdout=stdout,
        stderr=subprocess.PIPE,
        text=False,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        stderr = (result.stderr or b"").decode(errors="replace").strip()
        raise RuntimeError(stderr or f"command failed: {' '.join(cmd)}")
    return result


def load_json(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def merge_common(worker_config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    common = worker_config.get("workers-common", {})
    workers = worker_config.get("workers", {})
    merged = {}
    for name, cfg in workers.items():
        full = dict(common)
        full.update(cfg)
        merged[name] = full
    return merged


# ==========================================
# WORKER SELECTION
# ==========================================
def workers_from_etcd(args) -> Dict[str, Dict[str, Any]]:
    client_args = {"host": args.etcd_host, "port": args.etcd_port}
    if args.etcd_user and args.etcd_password:
        client_args.update(user=args.etcd_user, password=args.etcd_password)
    if args.etcd_ca_cert:
        client_args["ca_cert"] = args.etcd_ca_cert
    client = etcd3.client(**client_args)
    client.status()

    workers = {}
    for value, meta in client.get_prefix("/config/workers/"):
        name = meta.key.decode("utf-8").rsplit("/", 1)[-1]
        workers[name] = json.loads(value.decode("utf-8"))
    return workers


def selected_workers(workers: Dict[str, Dict[str, Any]], names: Iterable[str] | None):
    if not names:
        return workers

    selected = {}
    missing = []
    for name in names:
        if name not in workers:
            missing.append(name)
        else:
            selected[name] = workers[name]

    if missing:
        raise RuntimeError(f"unknown worker(s): {', '.join(missing)}")
    return selected


# ==========================================
# IMAGE DISTRIBUTION
# ==========================================
def ensure_local_image(image: str) -> None:
    run(["docker", "image", "inspect", image], check=True, stdout=subprocess.DEVNULL)


def write_image_archive(image: str, archive_path: Path) -> None:
    log.info(f"📦 Saving local image '{image}' to {archive_path}")
    with open(archive_path, "wb") as raw_out:
        docker_save = subprocess.Popen(
            ["docker", "save", image],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert docker_save.stdout is not None
        with gzip.GzipFile(fileobj=raw_out, mode="wb") as gz_out:
            for chunk in iter(lambda: docker_save.stdout.read(1024 * 1024), b""):
                gz_out.write(chunk)

        stderr = docker_save.stderr.read() if docker_save.stderr else b""
        rc = docker_save.wait()
        if rc != 0:
            raise RuntimeError(stderr.decode(errors="replace").strip() or "docker save failed")


def worker_ssh_fields(name: str, worker: Dict[str, Any]) -> Tuple[str, str, str]:
    ssh_user = worker.get("ssh-user", "ubuntu")
    ssh_host = worker.get("ip", name)
    ssh_key = os.path.expanduser(worker.get("ssh-key", "~/.ssh/id_rsa"))
    return ssh_user, ssh_host, ssh_key


def run_ssh(ssh_user: str, ssh_host: str, ssh_key: str, remote_args: list[str], timeout: int):
    cmd = [
        "ssh",
        "-i",
        ssh_key,
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"ConnectTimeout={timeout}",
        "--",
        f"{ssh_user}@{ssh_host}",
        shlex.join(remote_args),
    ]
    return run(cmd, check=True, timeout=timeout + 30)


def copy_archive(
    archive_path: Path, ssh_user: str, ssh_host: str, ssh_key: str, remote_path: str, timeout: int
):
    cmd = [
        "scp",
        "-i",
        ssh_key,
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"ConnectTimeout={timeout}",
        str(archive_path),
        f"{ssh_user}@{ssh_host}:{remote_path}",
    ]
    return run(cmd, check=True, timeout=timeout + 120)


def load_on_worker(
    name: str,
    worker: Dict[str, Any],
    archive_path: Path,
    remote_path: str,
    timeout: int,
    keep_remote: bool,
) -> Tuple[str, bool, str]:
    ssh_user, ssh_host, ssh_key = worker_ssh_fields(name, worker)
    try:
        log.info(f"📤 {name}: copying image archive to {ssh_user}@{ssh_host}:{remote_path}")
        copy_archive(archive_path, ssh_user, ssh_host, ssh_key, remote_path, timeout)

        log.info(f"🐳 {name}: loading image into Docker")
        # Docker accepts gzip archives directly; avoid masking pipeline errors.
        run_ssh(ssh_user, ssh_host, ssh_key, ["docker", "load", "--input", remote_path], timeout)

        if not keep_remote:
            run_ssh(ssh_user, ssh_host, ssh_key, ["rm", "-f", "--", remote_path], timeout)

        return name, True, "loaded"
    except Exception as exc:
        return name, False, str(exc)


def distribute_image(workers, archive_path, args):
    """Load the image on all selected workers and report success/failure counts."""
    ok = 0
    fail = 0
    worker_count = min(args.threads, len(workers))
    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(
                load_on_worker,
                name,
                worker,
                archive_path,
                args.remote_path,
                args.timeout,
                args.keep_remote,
            ): name
            for name, worker in workers.items()
        }
        for future in concurrent.futures.as_completed(futures):
            name, success, message = future.result()
            if success:
                ok += 1
                log.info(f"✅ {name}: {message}")
            else:
                fail += 1
                log.error(f"❌ {name}: {message}")

    return ok, fail


# ==========================================
# MAIN
# ==========================================
def create_parser():
    """Define the command-line arguments without executing runtime operations."""
    parser = argparse.ArgumentParser(
        description="Copy a local Docker image to all NetSatBench workers and load it there."
    )
    parser.add_argument(
        "--image",
        required=True,
        help="Local Docker image tag, for example netsatbench-sat-container-ion:prototype",
    )
    parser.add_argument(
        "--worker-config", help="Worker config JSON. If omitted, workers are read from Etcd."
    )
    parser.add_argument("--workers", nargs="+", help="Optional worker names to target.")
    parser.add_argument(
        "--remote-path",
        default="/tmp/netsatbench-image.tar.gz",
        help="Remote archive path on each worker.",
    )
    parser.add_argument(
        "--keep-remote", action="store_true", help="Keep the uploaded archive on workers."
    )
    parser.add_argument(
        "-t", "--threads", type=int, default=4, help="Parallel workers (default: 4)."
    )
    parser.add_argument(
        "--timeout", type=int, default=60, help="SSH/SCP connection timeout in seconds."
    )
    parser.add_argument("--log-level", default="INFO")

    parser.add_argument("--etcd-host", default=os.getenv("ETCD_HOST", "127.0.0.1"))
    parser.add_argument("--etcd-port", type=int, default=int(os.getenv("ETCD_PORT", "2379")))
    parser.add_argument("--etcd-user", default=os.getenv("ETCD_USER"))
    parser.add_argument("--etcd-password", default=os.getenv("ETCD_PASSWORD"))
    parser.add_argument("--etcd-ca-cert", default=os.getenv("ETCD_CA_CERT"))
    return parser


def main() -> int:
    args = create_parser().parse_args()
    log.setLevel(args.log_level.upper())

    if args.threads < 1:
        log.error("❌ --threads must be >= 1")
        return 2

    try:
        ensure_local_image(args.image)
        if args.worker_config:
            workers = merge_common(load_json(Path(args.worker_config)))
        else:
            workers = workers_from_etcd(args)

        workers = selected_workers(workers, args.workers)
        if not workers:
            log.error("❌ No workers found.")
            return 1

        with tempfile.TemporaryDirectory(prefix="nsb-image-") as tmpdir:
            archive_path = Path(tmpdir) / "image.tar.gz"
            write_image_archive(args.image, archive_path)

            log.info(f"🔎 Target workers: {', '.join(sorted(workers))}")
            ok, fail = distribute_image(workers, archive_path, args)

            if fail:
                log.error(f"❌ Image load completed with {fail} failure(s).")
                return 1

        log.info(f"👍 Image '{args.image}' is available on {ok} worker(s).")
        return 0
    except Exception as exc:
        log.error(f"❌ {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
