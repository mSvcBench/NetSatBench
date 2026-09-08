import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


PREPARE_PATH = Path(__file__).parents[1] / "control" / "nsb-ion-prepare.py"
sys.modules.setdefault("etcd3", types.SimpleNamespace())
MODULE_SPEC = importlib.util.spec_from_file_location("nsb_ion_prepare", PREPARE_PATH)
prepare = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(prepare)


class IonPrepareTests(unittest.TestCase):
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
            errors = prepare.rollback_preparation(
                ["sat1", "sat2"], {"sat1"}, workers, args
            )
        self.assertEqual(errors, [])
        stop_local.assert_called_once_with("sat1")
        stop_remote.assert_called_once_with("sat2", workers["sat2"], args)


if __name__ == "__main__":
    unittest.main()
