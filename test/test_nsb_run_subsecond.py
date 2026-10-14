import importlib.util
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


sys.modules.setdefault("etcd3", types.SimpleNamespace())

watchdog = types.ModuleType("watchdog")
watchdog_events = types.ModuleType("watchdog.events")
watchdog_observers = types.ModuleType("watchdog.observers")
watchdog_events.FileSystemEventHandler = object
watchdog_observers.Observer = object
sys.modules.setdefault("watchdog", watchdog)
sys.modules.setdefault("watchdog.events", watchdog_events)
sys.modules.setdefault("watchdog.observers", watchdog_observers)

RUNNER_PATH = Path(__file__).parents[1] / "control" / "nsb-run.py"
MODULE_SPEC = importlib.util.spec_from_file_location("nsb_run", RUNNER_PATH)
runner = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(runner)


class SubsecondEpochRunnerTests(unittest.TestCase):
    def test_iso_parser_and_formatter_preserve_milliseconds(self):
        value = runner.parse_utc_datetime("2026-01-01T00:00:10.375Z")
        self.assertEqual(value.microsecond, 375_000)
        self.assertEqual(
            runner.format_iso_z(value), "2026-01-01T00:00:10.375Z"
        )
        self.assertEqual(
            runner.convert_time_epoch_to_timestamp(
                "2026-01-01T00:00:10.375Z"
            ),
            datetime(2026, 1, 1, 0, 0, 10, 375_000, tzinfo=timezone.utc).timestamp(),
        )

    def test_numeric_epoch_time_preserves_milliseconds(self):
        self.assertEqual(runner.epoch_timestamp({"epoch-time": 10.375}), 10.375)

    def test_submillisecond_times_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "millisecond precision"):
            runner.parse_utc_datetime("2026-01-01T00:00:10.375500Z")
        with self.assertRaisesRegex(ValueError, "millisecond precision"):
            runner.epoch_timestamp({"epoch-time": 10.3755})

    def test_absolute_wait_keeps_fractional_target(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        first_epoch = 100.0
        epoch = 110.375
        target = start.timestamp() + 10.375
        with patch.object(runner.time, "time", return_value=target - 0.125), patch.object(
            runner.time, "sleep"
        ) as sleep:
            runner.wait_until_epoch_time(epoch, first_epoch, start, "epoch.json")
        sleep.assert_called_once_with(0.125)

    def test_absolute_schedule_rejects_incompatible_replay_modes(self):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        invalid = (
            (0.1, None, False, "fixed-wait"),
            (-1, 1.0, False, "loop-delay"),
            (-1, None, True, "resume"),
        )
        for fixed_wait, loop_delay, resume, expected in invalid:
            with self.subTest(expected=expected), self.assertRaisesRegex(
                ValueError, expected
            ):
                runner.validate_schedule_options(
                    start, fixed_wait, loop_delay, resume
                )

        runner.validate_schedule_options(start, -1, None, False)
        runner.validate_schedule_options(None, 0.1, 1.0, True)

    def test_etcd_connection_keeps_ca_without_credentials(self):
        client = types.SimpleNamespace(status=lambda: None)
        with patch.object(
            runner.etcd3, "client", return_value=client, create=True
        ) as factory:
            self.assertIs(
                runner.connect_etcd("etcd.example", 2379, etcd_ca_cert="ca.pem"),
                client,
            )
        factory.assert_called_once_with(
            host="etcd.example", port=2379, ca_cert="ca.pem"
        )


class FakeEtcd:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key), None

    def put(self, key, value):
        self.values[key] = value.encode("utf-8")

    def delete(self, key):
        self.values.pop(key, None)


class EpochLinkContractTests(unittest.TestCase):
    def setUp(self):
        self.etcd = FakeEtcd()
        self.etcd.put("/config/epoch-config", "{}")
        runner.etcd_client = self.etcd
        runner.PARALLEL_WORKERS = 1

    def apply_epoch(self, epoch):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "epoch.json"
            path.write_text(json.dumps(epoch), encoding="utf-8")
            runner.process_epoch_from_queue(str(path))

    def test_endpoint_antennas_select_interfaces_and_vni(self):
        link = {
            "endpoint1": "usr1",
            "endpoint2": "sat2",
            "endpoint1_antenna": 2,
            "endpoint2_antenna": 3,
            "delay": "4ms",
        }
        self.apply_epoch({"time": "2026-01-01T00:00:00Z", "links-add": [link]})

        usr_key = "/config/links/usr1/vl_sat2_3"
        sat_key = "/config/links/sat2/vl_usr1_2"
        self.assertIn(usr_key, self.etcd.values)
        self.assertIn(sat_key, self.etcd.values)
        expected_vni = runner.calculate_vni("usr1", 2, "sat2", 3)
        self.assertEqual(json.loads(self.etcd.values[usr_key])["vni"], expected_vni)

        self.apply_epoch({"time": "2026-01-01T00:00:01Z", "links-del": [link]})
        self.assertNotIn(usr_key, self.etcd.values)
        self.assertNotIn(sat_key, self.etcd.values)

    def test_partial_link_update_preserves_unspecified_properties(self):
        original = {
            "endpoint1": "sat1",
            "endpoint2": "sat2",
            "rate": "100mbit",
            "delay": "4ms",
            "loss": 0,
            "limit": 1000,
        }
        self.apply_epoch({"time": "2026-01-01T00:00:00Z", "links-add": [original]})
        self.apply_epoch({
            "time": "2026-01-01T00:00:01Z",
            "links-update": [{
                "endpoint1": "sat1",
                "endpoint2": "sat2",
                "delay": "6ms",
            }],
        })

        for key in (
            "/config/links/sat1/vl_sat2_1",
            "/config/links/sat2/vl_sat1_1",
        ):
            updated = json.loads(self.etcd.values[key])
            self.assertEqual(updated["delay"], "6ms")
            self.assertEqual(updated["rate"], "100mbit")
            self.assertEqual(updated["loss"], 0)
            self.assertEqual(updated["limit"], 1000)

    def test_invalid_antenna_identifier_is_rejected(self):
        for antenna in (0, -1, 1.5, True, "not-an-integer"):
            with self.subTest(antenna=antenna), self.assertRaisesRegex(
                ValueError, "positive integer"
            ):
                runner.link_antennas({"endpoint1_antenna": antenna})


if __name__ == "__main__":
    unittest.main()
