import importlib.util
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.modules.setdefault("etcd3", types.SimpleNamespace())
sys.path.insert(0, str(ROOT / "control"))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


loader = load("image_loader_safety", "utils/nsb-load-image.py")
prepare = load("ion_prepare_safety", "control/nsb-ion-prepare.py")
deploy = load("node_deploy_safety", "control/nsb-deploy.py")
restart = load("node_restart_safety", "control/nsb-node-restart.py")


class CommandSafetyTests(unittest.TestCase):
    def test_runtime_staging_only_replaces_managed_directories(self):
        required = (
            "ionadmin ionsecadmin bpadmin ipnadmin "
            "bpsendfile bpsendtest bprecvfile bpsource bpsink "
            "bpstats bpwatch rfxclock bpclock bptransit bpclm "
            "ipnfw ipnadminep udpcli udpclo cpsd ionwarn killm"
        ).split()
        script = ROOT / "sat-container" / "stage-ion-runtime.sh"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prefix = root / "prefix"
            output = root / "runtime"
            (prefix / "bin").mkdir(parents=True)
            (prefix / "lib").mkdir()
            for name in required:
                binary = prefix / "bin" / name
                binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                binary.chmod(0o755)

            environment = {**os.environ, "ION_PREFIX": str(prefix)}
            subprocess.run([script, output], env=environment, check=True)
            self.assertTrue((output / ".netsatbench-ion-runtime").is_file())
            subprocess.run([script, output], env=environment, check=True)

            unmanaged = root / "unmanaged"
            unmanaged.mkdir()
            sentinel = unmanaged / "keep"
            sentinel.write_text("user data", encoding="utf-8")
            result = subprocess.run(
                [script, unmanaged], env=environment, capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("unmanaged directory", result.stderr)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "user data")

    def test_etcd_ca_is_preserved_without_username_and_password(self):
        args = types.SimpleNamespace(
            etcd_host="etcd.example",
            etcd_port=2379,
            etcd_user=None,
            etcd_password=None,
            etcd_ca_cert="ca.pem",
        )
        client = types.SimpleNamespace(status=lambda: None, get_prefix=lambda _: [])

        for module, function in (
            (loader, loader.workers_from_etcd),
            (prepare, prepare.connect_etcd),
        ):
            with self.subTest(module=module.__name__), patch.object(
                module.etcd3, "client", return_value=client, create=True
            ) as factory:
                function(args)
            factory.assert_called_once_with(
                host="etcd.example", port=2379, ca_cert="ca.pem"
            )

        for module in (deploy, restart):
            with self.subTest(module=module.__name__), patch.object(
                module.etcd3, "client", return_value=client, create=True
            ) as factory:
                module.connect_etcd("etcd.example", 2379, etcd_ca_cert="ca.pem")
            factory.assert_called_once_with(
                host="etcd.example", port=2379, ca_cert="ca.pem"
            )

    def test_ca_only_configuration_is_propagated_to_node_containers(self):
        for module in (deploy, restart):
            remote_calls = []

            def remote(**kwargs):
                remote_calls.append(kwargs["remote_args"])
                return types.SimpleNamespace(stdout="", returncode=0, stderr="")

            with self.subTest(module=module.__name__), patch.object(
                module, "run_ssh", side_effect=remote
            ), patch.object(
                module.subprocess,
                "run",
                return_value=types.SimpleNamespace(returncode=0, stderr=""),
            ) as scp:
                kwargs = dict(
                    node_name="sat1",
                    worker="worker1",
                    ssh_username="ubuntu",
                    ssh_key_path="key",
                    worker_bridge="br0",
                    container_image="node:latest",
                    image_pull_policy="never",
                    cpu_requested=0,
                    mem_requested="0MiB",
                    cpu_limit=0,
                    mem_limit="0MiB",
                    etcd_host="etcd.example",
                    etcd_port=2379,
                    etcd_ca_cert="ca.pem",
                )
                if module is deploy:
                    kwargs["extra_env"] = None
                module.recreate_and_run_container(**kwargs)

            docker_run = next(call for call in remote_calls if call[:3] == ["docker", "run", "-d"])
            self.assertIn("ETCD_CA_CERT=/app/etcd-ca.crt", docker_run)
            self.assertFalse(any(item.startswith("ETCD_USER=") for item in docker_run))
            self.assertTrue(any(call[:2] == ["docker", "cp"] for call in remote_calls))
            scp.assert_called_once()

    def test_ssh_preserves_literal_arguments(self):
        value = "/tmp/archive with spaces; $(false) 'quote'.gz"
        with patch.object(loader, "run") as run:
            loader.run_ssh("user", "host", "key", ["printf", "%s", value], 5)
        command = run.call_args.args[0]
        self.assertEqual(command[-3:-1], ["--", "user@host"])
        result = subprocess.run(["sh", "-c", command[-1]], capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout, value)

    def test_image_load_uses_archive_without_pipeline(self):
        remote = "/tmp/image with spaces.gz"
        with patch.object(loader, "copy_archive"), patch.object(loader, "run_ssh") as ssh:
            result = loader.load_on_worker("worker", {}, Path("archive"), remote, 5, False)
        self.assertTrue(result[1])
        self.assertEqual(ssh.call_args_list[0].args[3], ["docker", "load", "--input", remote])
        self.assertEqual(ssh.call_args_list[1].args[3], ["rm", "-f", "--", remote])

    def test_failed_load_is_not_reported_successful_or_cleaned(self):
        with patch.object(loader, "copy_archive"), patch.object(loader, "run_ssh", side_effect=RuntimeError("load failed")) as ssh:
            result = loader.load_on_worker("worker", {}, Path("archive"), "/tmp/image.gz", 5, False)
        self.assertFalse(result[1])
        self.assertEqual(ssh.call_count, 1)

    def test_startup_stops_at_each_failed_admin(self):
        for failed in ("ionadmin", "ionsecadmin", "bpadmin", None):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as directory:
                script = prepare.ion_start_commands("sat1").replace("cd /ion/sat1", "cd " + shlex.quote(directory))
                definitions = "\n".join(
                    f"{name}() {{ echo {name} >> calls; return {7 if name == failed else 0}; }}"
                    for name in ("ionadmin", "ionsecadmin", "bpadmin")
                )
                result = subprocess.run(["sh", "-c", definitions + "\n" + script])
                calls = (Path(directory) / "calls").read_text().splitlines()
                expected = ["ionadmin", "ionsecadmin", "bpadmin"]
                if failed:
                    expected = expected[:expected.index(failed) + 1]
                self.assertEqual(calls, expected)
                self.assertEqual(result.returncode, 7 if failed else 0)

    def test_remote_startup_uses_shared_fail_fast_script(self):
        args = types.SimpleNamespace(dry_run=False, no_stop=True, no_start=False, ssh_timeout=5)
        with patch.object(prepare, "copy_to_worker"), patch.object(prepare, "run_worker_ssh") as ssh:
            prepare.prepare_remote_node("sat1", {"name": "worker", "host": "192.0.2.1"}, Path("archive"), args)
        self.assertIn(shlex.quote(prepare.ion_start_commands("sat1")), ssh.call_args_list[0].args[1])

    def test_runtime_check_handles_scripts_and_rejects_missing_dependencies(self):
        for output, code, expected in (("libici.so => /lib/libici.so", 0, 0), ("libici.so => not found", 0, 1), ("not a dynamic executable", 1, 0)):
            with self.subTest(output=output), tempfile.TemporaryDirectory() as directory:
                # The checker only locates these stubs; it must never run them.
                names = "ionadmin ionsecadmin bpadmin ipnadmin bpsendfile bpsendtest bprecvfile bpsource bpsink bpstats bpwatch rfxclock bpclock bptransit bpclm ipnfw ipnadminep udpcli udpclo cpsd ionwarn killm".split()
                for name in names:
                    path = Path(directory) / name
                    path.write_text("#!/bin/sh\nexit 99\n")
                    path.chmod(0o755)
                ldd = Path(directory) / "ldd"
                ldd.write_text(f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(output)}\nexit {code}\n")
                ldd.chmod(0o755)
                result = subprocess.run(["/bin/bash", str(ROOT / "sat-container/verify-ion-runtime.sh")], env=dict(os.environ, PATH=directory), capture_output=True)
                self.assertEqual(result.returncode, expected)

    def test_runtime_check_rejects_an_uninspectable_non_script(self):
        with tempfile.TemporaryDirectory() as directory:
            names = "ionadmin ionsecadmin bpadmin ipnadmin bpsendfile bpsendtest bprecvfile bpsource bpsink bpstats bpwatch rfxclock bpclock bptransit bpclm ipnfw ipnadminep udpcli udpclo cpsd ionwarn killm".split()
            for name in names:
                path = Path(directory) / name
                path.write_text("not an executable format\n")
                path.chmod(0o755)
            ldd = Path(directory) / "ldd"
            ldd.write_text("#!/bin/sh\necho loader-error >&2\nexit 1\n")
            ldd.chmod(0o755)
            result = subprocess.run(
                ["/bin/bash", str(ROOT / "sat-container/verify-ion-runtime.sh")],
                env=dict(os.environ, PATH=directory),
                capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"Cannot inspect ionadmin", result.stderr)

    def test_runtime_check_rejects_missing_executables(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                ["/bin/bash", str(ROOT / "sat-container/verify-ion-runtime.sh")],
                env=dict(os.environ, PATH=directory), capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"Missing ION executable", result.stderr)


if __name__ == "__main__":
    unittest.main()
