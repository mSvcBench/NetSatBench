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


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


loader = load("image_loader_safety", "utils/nsb-load-image.py")
prepare = load("ion_prepare_safety", "control/nsb-ion-prepare.py")


class CommandSafetyTests(unittest.TestCase):
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

    def test_runtime_check_rejects_missing_dependencies_and_loader_failure(self):
        for output, code, expected in (("libici.so => /lib/libici.so", 0, 0), ("libici.so => not found", 0, 1), ("loader error", 1, 1)):
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
