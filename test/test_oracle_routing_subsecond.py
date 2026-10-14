import argparse
import importlib.util
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scipy.sparse import lil_matrix


sys.modules.setdefault("etcd3", types.SimpleNamespace())
ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "oracle_routing_subsecond", ROOT / "utils" / "oracle-routing.py"
)
oracle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(oracle)


class OracleRoutingSubsecondTests(unittest.TestCase):
    def test_time_parser_and_formatter_preserve_milliseconds(self):
        value = oracle.parse_epoch_time("2026-01-01T00:00:10.375Z")
        self.assertEqual(value.microsecond, 375_000)
        self.assertEqual(
            oracle.format_epoch_time(value), "2026-01-01T00:00:10.375Z"
        )
        self.assertEqual(
            oracle.format_epoch_time(
                datetime(2026, 1, 1, 0, 0, 10, tzinfo=timezone.utc)
            ),
            "2026-01-01T00:00:10Z",
        )

    def test_submillisecond_times_and_offsets_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "millisecond precision"):
            oracle.parse_epoch_time("2026-01-01T00:00:10.375500Z")
        with self.assertRaises(argparse.ArgumentTypeError):
            oracle.parse_millisecond_offset("0.0005")
        with self.assertRaises(argparse.ArgumentTypeError):
            oracle.parse_millisecond_offset("-0.001")

    def test_route_epoch_shift_retains_fractional_time(self):
        epoch = {
            "time": "2026-01-01T00:00:10.375Z",
            "links-add": [{"endpoint1": "sat1", "endpoint2": "sat2"}],
        }
        result = oracle.compute_routes_single_epoch(
            epoch_data=epoch,
            node_map={"sat1": 0, "sat2": 1},
            node_type={"sat1": "sat", "sat2": "sat"},
            A_lil=lil_matrix((2, 2), dtype=float),
            node_to_route=[],
            node_to_install=[],
            previous_next_hops={},
            drain_before_break=False,
            offset_seconds=0.125,
            num_nodes=2,
            inv_node_map={0: "sat1", 1: "sat2"},
            ip_map={},
            ip_version=4,
            redundancy=False,
            routing_metric="hops",
            max_routes_per_epoch=50,
            sleep_seconds=1,
        )
        self.assertEqual(result["time"], "2026-01-01T00:00:10.250Z")


if __name__ == "__main__":
    unittest.main()
