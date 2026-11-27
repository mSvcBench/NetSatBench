"""Compatibility checks for upstream runtime changes and the ION extension."""
import importlib.util
import json
from pathlib import Path
import shlex
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


deploy = load("upstream_deploy", "control/nsb-deploy.py")
restart = load("upstream_restart", "control/nsb-node-restart.py")
runner = load("upstream_runner", "control/nsb-run.py")
execute = load("upstream_exec", "utils/nsb-exec.py")
exectype = load("upstream_exectype", "utils/nsb-exectype.py")
oracle = load("upstream_oracle", "utils/oracle-routing.py")


class UpstreamIntegrationTests(unittest.TestCase):
    def container_args(self):
        return dict(
            node_name="sat1", worker="192.0.2.10", ssh_username="ubuntu",
            ssh_key_path="key", worker_bridge="sat-vnet",
            container_image="ion:subsecond", image_pull_policy="never",
            cpu_requested=0.1, mem_requested="64MiB", cpu_limit=0.2,
            mem_limit="400MiB", etcd_host="192.0.2.20", etcd_port=2379,
            extra_env={"ION_RFXCLOCK_POLL_MSEC": 10},
            volume="/worker data:/app/data:ro",
        )

    def test_deploy_and_recreate_keep_volume_and_ion_timing(self):
        for module in (deploy, restart):
            with self.subTest(module=module.__name__), patch.object(
                module, "run_ssh", return_value=types.SimpleNamespace(stdout="")
            ) as ssh:
                module.recreate_and_run_container(**self.container_args())
                commands = [c.kwargs["remote_args"] for c in ssh.call_args_list]
                command = next(c for c in commands if c[:2] == ["docker", "run"])
                self.assertEqual(command[command.index("--volume") + 1], "/worker data:/app/data:ro")
                self.assertIn("ION_RFXCLOCK_POLL_MSEC=10", command)
                self.assertIn("--pull=never", command)
                self.assertEqual(command[-1], "ion:subsecond")

    def test_soft_restart_does_not_remove_or_recreate_existing_container(self):
        with patch.object(restart, "run_ssh", return_value=types.SimpleNamespace(stdout="sat1\n")) as ssh:
            restart.recreate_and_run_container(**self.container_args(), soft=True)
        commands = [c.kwargs["remote_args"] for c in ssh.call_args_list]
        self.assertEqual(commands[-1], ["docker", "restart", "sat1"])
        self.assertEqual(len(commands), 2)

    def test_node_configuration_passes_env_and_volume_to_both_paths(self):
        node = {"worker": "host-1", "image": "ion:subsecond", "image-pull-policy": "never",
                "env": {"ION_RFXCLOCK_POLL_MSEC": 10}, "volume": "/worker:/app/data"}
        workers = {"host-1": {"ip": "192.0.2.10"}}
        for module in (deploy, restart):
            with self.subTest(module=module.__name__), patch.object(module, "recreate_and_run_container") as create:
                result = module.create_one_node("sat1", node, workers, "192.0.2.20", 2379)
                self.assertTrue(result[1], result)
                self.assertEqual(create.call_args.kwargs["extra_env"], node["env"])
                self.assertEqual(create.call_args.kwargs["volume"], node["volume"])
                node["env"] = "invalid"
                self.assertFalse(module.create_one_node("sat1", node, workers, "192.0.2.20", 2379)[1])
                node["env"] = {"ION_RFXCLOCK_POLL_MSEC": 10}

    def test_empty_queue_never_prompts(self):
        with tempfile.TemporaryDirectory() as directory, patch("builtins.input") as prompt:
            self.assertTrue(runner.prepare_epoch_queue(Path(directory), "ask"))
            prompt.assert_not_called()

    def test_unattended_stale_queue_fails_without_deleting_entries(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(runner.sys.stdin, "isatty", return_value=False), patch("builtins.input") as prompt:
            entry = Path(directory) / "old.json"
            entry.write_text("{}")
            self.assertFalse(runner.prepare_epoch_queue(Path(directory), "ask"))
            self.assertTrue(entry.exists())
            prompt.assert_not_called()

    def test_explicit_queue_clear_never_follows_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            queue = root / "epoch-queue"
            queue.mkdir()
            external = root / "keep.txt"
            external.write_text("user data")
            (queue / "old.json").write_text("{}")
            (queue / "link").symlink_to(external)
            self.assertTrue(runner.prepare_epoch_queue(queue, "clear"))
            self.assertEqual(list(queue.iterdir()), [])
            self.assertEqual(external.read_text(), "user data")

    def test_keep_and_abort_preserve_stale_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            entry = Path(directory) / "old.json"
            entry.write_text("{}")
            self.assertTrue(runner.prepare_epoch_queue(Path(directory), "keep"))
            self.assertFalse(runner.prepare_epoch_queue(Path(directory), "abort"))
            self.assertTrue(entry.exists())

    def test_queue_cleanup_error_prevents_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "old.json").write_text("{}")
            with patch.object(Path, "unlink", side_effect=PermissionError("denied")):
                self.assertFalse(runner.prepare_epoch_queue(Path(directory), "clear"))

    def test_queue_prompt_eof_preserves_entries(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(runner.sys.stdin, "isatty", return_value=True), patch("builtins.input", side_effect=EOFError):
            entry = Path(directory) / "old.json"
            entry.write_text("{}")
            self.assertFalse(runner.prepare_epoch_queue(Path(directory), "ask"))
            self.assertTrue(entry.exists())

    def test_readiness_checks_first_node_of_etcd_iterator(self):
        metadata = types.SimpleNamespace(key=b"/config/nodes/sat1")
        client = types.SimpleNamespace(get_prefix=lambda _: iter([(b"{}", metadata)]))
        with patch.object(sys, "argv", ["nsb-run", "--epoch-dir", "/unused", "--file-pattern", "*.json"]), patch.object(runner, "connect_etcd", return_value=client), patch.object(runner, "start_queue_watcher") as watcher:
            self.assertEqual(runner.main(), 1)
            watcher.assert_not_called()

    def test_quoted_exec_keeps_shell_commands_inside_container(self):
        client = types.SimpleNamespace(get=lambda _: (json.dumps({"ip": "192.0.2.10", "ssh-user": "ubuntu", "ssh-key": "/key"}).encode(), None))
        cases = [
            (["printf 'a; b'; printf '%s' '$HOME'"], ["bash", "-c", "printf 'a; b'; printf '%s' '$HOME'"]),
            (["printf", "%s", "a; b", "$(not-a-host-command)"], ["printf", "%s", "a; b", "$(not-a-host-command)"]),
        ]
        for command, expected in cases:
            with self.subTest(command=command), patch.object(sys, "argv", ["nsb-exec", "sat1", *command]), patch.object(execute.etcd3, "client", return_value=client, create=True), patch.object(execute, "get_node_cfg", return_value={"worker": "host-1"}), patch.object(execute.subprocess, "run", return_value=types.SimpleNamespace(returncode=0)) as run:
                with self.assertRaises(SystemExit) as exit_code:
                    execute.main()
                self.assertEqual(exit_code.exception.code, 0)
                ssh_command = run.call_args.args[0]
                self.assertEqual(shlex.split(ssh_command[-1]), ["docker", "exec", "sat1", *expected])

    def test_exectype_reports_all_failed_nodes(self):
        client = types.SimpleNamespace()
        with patch.object(sys, "argv", ["nsb-exectype", "satellite", "true"]), patch.object(exectype.etcd3, "client", return_value=client, create=True), patch.object(exectype, "get_nodes_by_type", return_value=["sat2", "sat1"]), patch.object(exectype, "exec_on_node", side_effect=lambda _, node: (7 if node == "sat1" else 9, ["failed-" + node])) as run, self.assertLogs(exectype.log, level="ERROR") as logs:
            self.assertEqual(exectype.main(), 7)
        self.assertEqual(run.call_count, 2)
        self.assertIn("failed-sat1", "\n".join(logs.output))
        self.assertIn("failed-sat2", "\n".join(logs.output))

    def test_oracle_splits_route_batches_even_without_sleep(self):
        commands = ["route1", "route2", "route3"]
        self.assertEqual(oracle.split_route_commands_with_sleep(commands, 2, 0), ["route1; route2", "route3"])
        self.assertEqual(oracle.split_route_commands_with_sleep(commands, 2, 1), ["route1; route2", "sleep 1", "route3"])
        self.assertEqual(oracle.split_route_commands_with_sleep(commands, 0, 1), ["route1; route2; route3"])


if __name__ == "__main__":
    unittest.main()
