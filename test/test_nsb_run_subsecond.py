import importlib.util
import sys
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


if __name__ == "__main__":
    unittest.main()
