import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "nsb_stats_under_test", ROOT / "utils/nsb-stats.py"
)
stats = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stats)


class StatisticsCompatibilityTests(unittest.TestCase):
    def test_epoch_parser_preserves_milliseconds(self):
        value = stats.convert_time_epoch_to_timestamp(
            "2026-01-01T00:00:00.375Z"
        )
        expected = datetime(
            2026, 1, 1, 0, 0, 0, 375_000, tzinfo=timezone.utc
        ).timestamp()
        self.assertEqual(value, expected)

    def test_metis_assignment_writes_worker_field(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sat-config.json"
            workers = root / "worker-config.json"
            output = root / "sat-config-metis.json"
            source.write_text(
                json.dumps({
                    "epoch-config": {"epoch-dir": "epochs"},
                    "nodes": {
                        "sat1": {"type": "satellite"},
                        "sat2": {"type": "satellite"},
                    },
                }),
                encoding="utf-8",
            )
            workers.write_text(
                json.dumps({"workers": {"worker-a": {}, "worker-b": {}}}),
                encoding="utf-8",
            )

            stats.apply_metis_worker_assignment(
                str(source),
                str(output),
                {"sat1": 0, "sat2": 1},
                str(workers),
            )

            generated = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(generated["nodes"]["sat1"]["worker"], "worker-a")
            self.assertEqual(generated["nodes"]["sat2"]["worker"], "worker-b")
            self.assertEqual(generated["epoch-config"], {"epoch-dir": "epochs"})


if __name__ == "__main__":
    unittest.main()
