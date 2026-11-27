import importlib.util
import shlex
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


PREPARE_PATH = Path(__file__).parents[1] / "control" / "nsb-ion-prepare.py"
sys.modules.setdefault("etcd3", types.SimpleNamespace())
MODULE_SPEC = importlib.util.spec_from_file_location("nsb_ion_prepare", PREPARE_PATH)
prepare = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(prepare)


class IonPrepareTests(unittest.TestCase):
    def test_fractional_guard_and_timestamp_helpers_preserve_milliseconds(self):
        self.assertEqual(prepare.parse_millisecond_seconds("0.863"), 0.863)
        timestamp = datetime(2030, 1, 1, 0, 0, 3, 742000, tzinfo=timezone.utc)
        self.assertEqual(prepare.format_iso_z(timestamp), "2030-01-01T00:00:03.742Z")

    def test_node_names_are_restricted_before_remote_shell_use(self):
        self.assertEqual(prepare.validate_node_name("sat-12.example"), "sat-12.example")
        for invalid in ("sat1;id", "sat 1", "", "../sat1"):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(RuntimeError, "invalid container/node name"):
                    prepare.validate_node_name(invalid)

    def test_partial_preparation_rollback_stops_local_and_remote_nodes(self):
        args = SimpleNamespace(ssh_timeout=5)
        workers = {"sat2": {"name": "worker-2", "host": "192.0.2.2"}}
        with (
            patch.object(prepare, "stop_ion") as stop_local,
            patch.object(prepare, "stop_remote_ion") as stop_remote,
        ):
            errors = prepare.rollback_preparation(["sat1", "sat2"], {"sat1"}, workers, args)
        self.assertEqual(errors, [])
        stop_local.assert_called_once_with("sat1")
        stop_remote.assert_called_once_with("sat2", workers["sat2"], args)

    def test_failed_batch_waits_for_workers_and_rolls_back_all_targets(self):
        args = SimpleNamespace(jobs=2)
        workers = {"sat2": {"name": "worker-2"}}
        archives = {"sat2": Path("sat2.tar.gz")}
        with (
            patch.object(prepare, "prepare_node", side_effect=RuntimeError("startup failed")),
            patch.object(prepare, "prepare_remote_node", return_value="Prepared sat2") as remote,
            patch.object(prepare, "rollback_preparation", return_value=[]) as rollback,
        ):
            with self.assertRaisesRegex(RuntimeError, "sat1: startup failed"):
                prepare.prepare_selected_nodes(
                    args,
                    ["sat1", "sat2"],
                    Path("generated"),
                    "nodes",
                    {"sat1"},
                    workers,
                    archives,
                )
        remote.assert_called_once_with("sat2", workers["sat2"], archives["sat2"], args)
        rollback.assert_called_once_with(["sat1", "sat2"], {"sat1"}, workers, args)

    def test_failed_batch_reports_rollback_errors(self):
        args = SimpleNamespace(jobs=1)
        with (
            patch.object(prepare, "prepare_node", side_effect=RuntimeError("startup failed")),
            patch.object(prepare, "rollback_preparation", return_value=["sat1: stop failed"]),
        ):
            with self.assertRaisesRegex(RuntimeError, "rollback errors: sat1: stop failed"):
                prepare.prepare_selected_nodes(
                    args,
                    ["sat1"],
                    Path("generated"),
                    "nodes",
                    {"sat1"},
                    {},
                    {},
                )

    def test_local_transport_failure_does_not_contact_etcd(self):
        args = SimpleNamespace(transport="local")
        with (
            patch.object(prepare, "local_container_exists", return_value=False),
            patch.object(prepare, "connect_etcd") as connect,
        ):
            with self.assertRaisesRegex(RuntimeError, "containers are not available"):
                prepare.resolve_preparation_targets(
                    args,
                    ["sat1"],
                    Path("generated"),
                    "nodes",
                    Path("temp"),
                )
        connect.assert_not_called()

    def test_remote_preparation_uses_the_same_stop_script_as_local(self):
        args = SimpleNamespace(dry_run=False, no_stop=False, no_start=False, ssh_timeout=5)
        worker = {"name": "worker-1", "host": "192.0.2.1"}
        with (
            patch.object(prepare, "copy_to_worker"),
            patch.object(prepare, "run_worker_ssh") as ssh,
        ):
            prepare.prepare_remote_node("sat1", worker, Path("sat1.tar.gz"), args)
        command = shlex.split(ssh.call_args_list[0].args[1])
        self.assertIn(prepare.ion_stop_commands("sat1"), command)
        self.assertIn(prepare.ion_start_commands("sat1"), command)

    def test_generator_command_preserves_fractional_options(self):
        args = prepare.create_parser(prepare.repo_root_from_script()).parse_args(
            [
                "--epoch-duration",
                "0.863",
                "--contact-start-guard",
                "0.004",
                "--contact-end-guard",
                "0.001",
                "--clock-error",
                "0.002",
            ]
        )
        timestamp = datetime(2030, 1, 1, 0, 0, 0, 375000, tzinfo=timezone.utc)
        command = prepare.build_generator_command(
            args,
            Path("registry.json"),
            Path("epochs"),
            Path("output"),
            timestamp,
        )
        for option, value in {
            "--epoch-duration": "0.863",
            "--contact-start-guard": "0.004",
            "--contact-end-guard": "0.001",
            "--clock-error": "0.002",
            "--absolute-start-time": "2030-01-01T00:00:00.375Z",
        }.items():
            self.assertEqual(command[command.index(option) + 1], value)


if __name__ == "__main__":
    unittest.main()
