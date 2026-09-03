import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


GENERATOR_PATH = (
    Path(__file__).parents[1] / "utils" / "nsb-ion-generate-configs.py"
)
MODULE_SPEC = importlib.util.spec_from_file_location(
    "nsb_ion_generate_configs", GENERATOR_PATH
)
generator = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(generator)


class IonContactRateTests(unittest.TestCase):
    def setUp(self):
        self.registry = {
            "experiment": "rate-update-test",
            "udp_port": 1113,
            "default_contact_rate_bytes_per_second": 1_250_000,
            "owlt_seconds": 1,
            "nodes": {
                "sat1": {"ion_node_number": 1, "udp_cla_address": "172.100.0.2"},
                "sat2": {"ion_node_number": 2, "udp_cla_address": "172.100.0.6"},
            },
        }

    def write_epoch(self, directory, number, seconds, **actions):
        epoch = {
            "epoch-time": seconds,
            "links-add": actions.get("links_add", []),
            "links-update": actions.get("links_update", []),
            "links-del": actions.get("links_del", []),
        }
        path = Path(directory) / f"NetSatBench-epoch{number}.json"
        path.write_text(json.dumps(epoch), encoding="utf-8")

    def test_rate_parser_uses_decimal_network_units(self):
        self.assertEqual(generator.parse_rate_bytes_per_second("1mbit"), 125_000)
        self.assertEqual(generator.parse_rate_bytes_per_second("100kbit"), 12_500)
        self.assertEqual(generator.parse_rate_bytes_per_second("1.5gbit"), 187_500_000)

    def test_delay_parser_and_ion_quantization(self):
        self.assertAlmostEqual(generator.parse_delay_seconds("500us"), 0.0005)
        self.assertAlmostEqual(generator.parse_delay_seconds("4.7ms"), 0.0047)
        self.assertAlmostEqual(generator.parse_delay_seconds("1.2s"), 1.2)
        self.assertEqual(generator.quantize_ion_owlt_seconds(0.0047), 1)
        self.assertEqual(
            generator.quantize_ion_owlt_seconds(0.0047, "zero"), 0
        )
        self.assertEqual(
            generator.quantize_ion_owlt_seconds(
                0.0047, "ceil-millisecond"
            ),
            0.005,
        )
        self.assertEqual(
            generator.quantize_ion_owlt_seconds(
                0.000415, "ceil-millisecond"
            ),
            0.001,
        )
        self.assertEqual(generator.quantize_ion_owlt_seconds(1.2), 2)

    def test_subsecond_fork_policy_renders_physics_owlt_in_milliseconds(self):
        registry = {
            **self.registry,
            "owlt_seconds": 0.001,
            "ion_subsecond_owlt_policy": "ceil-millisecond",
        }
        with tempfile.TemporaryDirectory() as epoch_dir:
            self.write_epoch(
                epoch_dir,
                0,
                0,
                links_add=[{
                    "endpoint1": "sat1",
                    "endpoint2": "sat2",
                    "rate": "1mbit",
                    "delay": "4.902477ms",
                }],
            )
            self.write_epoch(epoch_dir, 1, 10)
            contacts = generator.derive_contacts_from_epochs(
                registry,
                epoch_dir,
                "NetSatBench-epoch*.json",
                epoch_duration=10,
            )

        self.assertEqual(contacts[0]["ion_owlt_seconds"], 0.005)
        rendered = generator.render_global_ionrc(
            {**registry, "contacts": contacts}
        )
        self.assertIn("a range +0 +20 1 2 0.005", rendered)
        self.assertNotIn("a range +0 +20 1 2 1\n", rendered)

    def test_fractional_physical_epochs_create_fractional_contacts(self):
        with tempfile.TemporaryDirectory() as epoch_dir:
            self.write_epoch(
                epoch_dir,
                0,
                10.000,
                links_add=[{
                    "endpoint1": "sat1",
                    "endpoint2": "sat2",
                    "rate": "1mbit",
                    "delay": "4.2ms",
                }],
            )
            self.write_epoch(
                epoch_dir,
                1,
                11.500,
                links_del=[{"endpoint1": "sat1", "endpoint2": "sat2"}],
            )
            contacts = generator.derive_contacts_from_epochs(
                {
                    **self.registry,
                    "ion_subsecond_owlt_policy": "ceil-millisecond",
                },
                epoch_dir,
                "NetSatBench-epoch*.json",
                epoch_duration=1.5,
                contact_start_guard=0.375,
                contact_end_guard=0.125,
            )

        self.assertEqual(contacts[0]["start"], 0.375)
        self.assertEqual(contacts[0]["end"], 1.375)
        rendered = generator.render_global_ionrc({
            **self.registry,
            "ion_subsecond_owlt_policy": "ceil-millisecond",
            "contacts": contacts,
        })
        self.assertIn("a contact +0.375 +1.375 1 2", rendered)
        self.assertIn("a range +0.375 +1.375 1 2 0.005", rendered)

    def test_epoch_offset_rejects_submillisecond_precision(self):
        with self.assertRaisesRegex(ValueError, "millisecond precision"):
            generator.epoch_offset_seconds(10.3755, 10.0, Path("epoch.json"))

    def test_registry_accepts_a_contact_shorter_than_one_second(self):
        registry = {
            **self.registry,
            "contacts": [{
                "from": "sat1",
                "to": "sat2",
                "start": 0.1,
                "end": 0.9,
                "rate_bytes_per_second": 1000,
                "ion_owlt_seconds": 0.005,
            }],
        }
        generator.validate_ion_node_registry(registry)

    def test_physics_policy_sets_ion_owlt_to_zero_without_changing_delay(self):
        registry = {**self.registry, "ion_subsecond_owlt_policy": "zero"}
        link = {
            "endpoint1": "sat1",
            "endpoint2": "sat2",
            "rate": "1mbit",
            "delay": "7.4ms",
        }

        self.assertAlmostEqual(generator.parse_delay_seconds(link["delay"]), 0.0074)
        self.assertEqual(
            generator.resolve_contact_owlt_seconds(registry, link), 0
        )

    def test_physics_policy_renders_zero_range_for_subsecond_epoch_delay(self):
        registry = {**self.registry, "ion_subsecond_owlt_policy": "zero"}
        with tempfile.TemporaryDirectory() as epoch_dir:
            self.write_epoch(
                epoch_dir,
                0,
                0,
                links_add=[{
                    "endpoint1": "sat1",
                    "endpoint2": "sat2",
                    "rate": "1mbit",
                    "delay": "7.4ms",
                }],
            )
            self.write_epoch(epoch_dir, 1, 10)
            contacts = generator.derive_contacts_from_epochs(
                registry,
                epoch_dir,
                "NetSatBench-epoch*.json",
                epoch_duration=10,
            )

        self.assertEqual(contacts[0]["ion_owlt_seconds"], 0)
        rendered = generator.render_global_ionrc(
            {**registry, "contacts": contacts}
        )
        self.assertIn("a range +0 +20 1 2 0", rendered)

    def test_invalid_subsecond_policy_is_rejected(self):
        registry = {
            **self.registry,
            "ion_subsecond_owlt_policy": "nearest",
            "contacts": [["sat1", "sat2"]],
        }
        with self.assertRaisesRegex(ValueError, "ion_subsecond_owlt_policy"):
            generator.validate_ion_node_registry(registry)

    def test_udp_outduct_payload_stays_below_overlay_mtu(self):
        registry = {
            **self.registry,
            "contacts": [{
                "from": "sat1",
                "to": "sat2",
                "start": 0,
                "end": 10,
                "rate_bytes_per_second": 125_000,
            }],
        }

        rendered = generator.render_node_bprc(registry, "sat1")

        self.assertIn(
            "a outduct udp 172.100.0.6:1113 udpclo 1200",
            rendered,
        )

    def test_ion_heap_supports_fragmented_queue_studies(self):
        rendered = generator.render_node_ionconfig(self.registry, "sat1")

        self.assertIn("heapWords 20000000", rendered)

    def test_link_update_splits_contact_at_new_rate(self):
        with tempfile.TemporaryDirectory() as epoch_dir:
            self.write_epoch(
                epoch_dir,
                0,
                0,
                links_add=[
                    {"endpoint1": "sat1", "endpoint2": "sat2", "rate": "1mbit"}
                ],
            )
            self.write_epoch(
                epoch_dir,
                1,
                10,
                links_update=[
                    {"endpoint1": "sat1", "endpoint2": "sat2", "rate": "100kbit"}
                ],
            )
            self.write_epoch(epoch_dir, 2, 20)

            contacts = generator.derive_contacts_from_epochs(
                self.registry,
                epoch_dir,
                "NetSatBench-epoch*.json",
                epoch_duration=10,
            )

        self.assertEqual(
            [
                (contact["start"], contact["end"], contact["rate_bytes_per_second"])
                for contact in contacts
            ],
            [(0, 10, 125_000), (10, 30, 12_500)],
        )

        rendered = generator.render_global_ionrc({**self.registry, "contacts": contacts})
        self.assertIn("a contact +0 +10 1 2 125000", rendered)
        self.assertIn("a contact +10 +30 1 2 12500", rendered)

    def test_explicit_ion_rate_is_independent_from_physical_wire_rate(self):
        with tempfile.TemporaryDirectory() as epoch_dir:
            self.write_epoch(
                epoch_dir,
                0,
                0,
                links_add=[{
                    "endpoint1": "sat1",
                    "endpoint2": "sat2",
                    "rate": "2.4mbit",
                    "ion-rate": "2mbit",
                }],
            )

            contacts = generator.derive_contacts_from_epochs(
                self.registry,
                epoch_dir,
                "NetSatBench-epoch*.json",
                epoch_duration=10,
            )

        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0]["rate_bytes_per_second"], 250_000)

    def test_explicit_ion_rate_update_splits_contact(self):
        with tempfile.TemporaryDirectory() as epoch_dir:
            self.write_epoch(
                epoch_dir,
                0,
                0,
                links_add=[{
                    "endpoint1": "sat1",
                    "endpoint2": "sat2",
                    "rate": "2.4mbit",
                    "ion-rate": "2mbit",
                }],
            )
            self.write_epoch(
                epoch_dir,
                1,
                10,
                links_update=[{
                    "endpoint1": "sat1",
                    "endpoint2": "sat2",
                    "ion-rate": "1mbit",
                }],
            )

            contacts = generator.derive_contacts_from_epochs(
                self.registry,
                epoch_dir,
                "NetSatBench-epoch*.json",
                epoch_duration=10,
            )

        self.assertEqual(
            [
                (row["start"], row["end"], row["rate_bytes_per_second"])
                for row in contacts
            ],
            [(0, 10, 250_000), (10, 20, 125_000)],
        )

    def test_update_without_rate_preserves_contact_rate(self):
        with tempfile.TemporaryDirectory() as epoch_dir:
            self.write_epoch(
                epoch_dir,
                0,
                0,
                links_add=[
                    {"endpoint1": "sat1", "endpoint2": "sat2", "rate": "1mbit"}
                ],
            )
            self.write_epoch(
                epoch_dir,
                1,
                10,
                links_update=[
                    {"endpoint1": "sat1", "endpoint2": "sat2", "delay": "20ms"}
                ],
            )

            contacts = generator.derive_contacts_from_epochs(
                self.registry,
                epoch_dir,
                "NetSatBench-epoch*.json",
                epoch_duration=10,
            )

        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0]["start"], 0)
        self.assertEqual(contacts[0]["end"], 20)
        self.assertEqual(contacts[0]["rate_bytes_per_second"], 125_000)
        self.assertEqual(contacts[0]["ion_owlt_seconds"], 1)

    def test_delay_update_splits_range_at_new_whole_second_owlt(self):
        with tempfile.TemporaryDirectory() as epoch_dir:
            self.write_epoch(
                epoch_dir,
                0,
                0,
                links_add=[
                    {
                        "endpoint1": "sat1",
                        "endpoint2": "sat2",
                        "rate": "1mbit",
                        "delay": "4.7ms",
                    }
                ],
            )
            self.write_epoch(
                epoch_dir,
                1,
                10,
                links_update=[
                    {
                        "endpoint1": "sat1",
                        "endpoint2": "sat2",
                        "delay": "1.2s",
                    }
                ],
            )

            contacts = generator.derive_contacts_from_epochs(
                self.registry,
                epoch_dir,
                "NetSatBench-epoch*.json",
                epoch_duration=10,
            )

        self.assertEqual(
            [
                (
                    contact["start"],
                    contact["end"],
                    contact["rate_bytes_per_second"],
                    contact["ion_owlt_seconds"],
                )
                for contact in contacts
            ],
            [
                (0, 10, 125_000, 1),
                (10, 20, 125_000, 2),
            ],
        )

        rendered = generator.render_global_ionrc(
            {**self.registry, "contacts": contacts}
        )
        self.assertIn("a range +0 +10 1 2 1", rendered)
        self.assertIn("a range +10 +20 1 2 2", rendered)

    def test_contact_guards_keep_ion_inside_physical_link_window(self):
        with tempfile.TemporaryDirectory() as epoch_dir:
            self.write_epoch(
                epoch_dir,
                0,
                0,
                links_add=[
                    {"endpoint1": "sat1", "endpoint2": "sat2", "rate": "1mbit"}
                ],
            )
            self.write_epoch(
                epoch_dir,
                1,
                45,
                links_del=[
                    {"endpoint1": "sat1", "endpoint2": "sat2"}
                ],
            )

            contacts = generator.derive_contacts_from_epochs(
                self.registry,
                epoch_dir,
                "NetSatBench-epoch*.json",
                epoch_duration=45,
                contact_start_guard=4,
                contact_end_guard=1,
            )

        self.assertEqual(
            [(contact["start"], contact["end"]) for contact in contacts],
            [(4, 44)],
        )

    def test_fractional_guards_are_preserved_in_absolute_ion_contacts(self):
        with tempfile.TemporaryDirectory() as epoch_dir:
            self.write_epoch(
                epoch_dir,
                0,
                0,
                links_add=[
                    {"endpoint1": "sat1", "endpoint2": "sat2", "rate": "1mbit"}
                ],
            )
            self.write_epoch(
                epoch_dir,
                1,
                45,
                links_del=[{"endpoint1": "sat1", "endpoint2": "sat2"}],
            )
            contacts = generator.derive_contacts_from_epochs(
                self.registry,
                epoch_dir,
                "NetSatBench-epoch*.json",
                epoch_duration=45,
                contact_start_guard=3.742,
                contact_end_guard=0.863,
            )

        registry = {
            **self.registry,
            "contacts": contacts,
            "_absolute_start_time": datetime(
                2030, 1, 1, tzinfo=timezone.utc
            ),
        }
        rendered = generator.render_global_ionrc(registry)
        self.assertEqual(contacts[0]["start"], 3.742)
        self.assertEqual(contacts[0]["end"], 44.137)
        self.assertIn(
            "a contact 2030/01/01-00:00:03.742 "
            "2030/01/01-00:00:44.137 1 2 125000",
            rendered,
        )

    def test_guard_parser_rejects_sub_millisecond_precision(self):
        self.assertEqual(generator.parse_millisecond_seconds("3.742"), 3.742)
        with self.assertRaisesRegex(Exception, "three decimal places"):
            generator.parse_millisecond_seconds("3.7421")

    def test_update_before_guard_changes_initial_contact_segment(self):
        with tempfile.TemporaryDirectory() as epoch_dir:
            self.write_epoch(
                epoch_dir,
                0,
                0,
                links_add=[
                    {"endpoint1": "sat1", "endpoint2": "sat2", "rate": "1mbit"}
                ],
            )
            self.write_epoch(
                epoch_dir,
                1,
                3,
                links_update=[
                    {"endpoint1": "sat1", "endpoint2": "sat2", "rate": "100kbit"}
                ],
            )

            contacts = generator.derive_contacts_from_epochs(
                self.registry,
                epoch_dir,
                "NetSatBench-epoch*.json",
                epoch_duration=45,
                contact_start_guard=4,
                contact_end_guard=1,
            )

        self.assertEqual(
            [
                (
                    contact["start"],
                    contact["end"],
                    contact["rate_bytes_per_second"],
                )
                for contact in contacts
            ],
            [(4, 47, 12_500)],
        )

    def test_contact_guards_reject_an_empty_physical_window(self):
        with tempfile.TemporaryDirectory() as epoch_dir:
            self.write_epoch(
                epoch_dir,
                0,
                0,
                links_add=[
                    {"endpoint1": "sat1", "endpoint2": "sat2", "rate": "1mbit"}
                ],
            )
            self.write_epoch(
                epoch_dir,
                1,
                5,
                links_del=[
                    {"endpoint1": "sat1", "endpoint2": "sat2"}
                ],
            )

            with self.assertRaisesRegex(
                ValueError, "contact guards eliminate physical link window"
            ):
                generator.derive_contacts_from_epochs(
                    self.registry,
                    epoch_dir,
                    "NetSatBench-epoch*.json",
                    epoch_duration=45,
                    contact_start_guard=4,
                    contact_end_guard=1,
                )

    def test_epoch_delete_for_inactive_link_is_rejected(self):
        with tempfile.TemporaryDirectory() as epoch_dir:
            self.write_epoch(
                epoch_dir,
                0,
                0,
                links_del=[{"endpoint1": "sat1", "endpoint2": "sat2"}],
            )
            with self.assertRaisesRegex(ValueError, "delete references inactive"):
                generator.derive_contacts_from_epochs(
                    self.registry,
                    epoch_dir,
                    "NetSatBench-epoch*.json",
                    epoch_duration=10,
                )

    def test_epoch_update_for_inactive_link_is_rejected(self):
        with tempfile.TemporaryDirectory() as epoch_dir:
            self.write_epoch(
                epoch_dir,
                0,
                0,
                links_update=[{
                    "endpoint1": "sat1",
                    "endpoint2": "sat2",
                    "rate": "1mbit",
                }],
            )
            with self.assertRaisesRegex(ValueError, "update references inactive"):
                generator.derive_contacts_from_epochs(
                    self.registry,
                    epoch_dir,
                    "NetSatBench-epoch*.json",
                    epoch_duration=10,
                )

    def test_epoch_duplicate_add_is_rejected(self):
        with tempfile.TemporaryDirectory() as epoch_dir:
            link = {"endpoint1": "sat1", "endpoint2": "sat2", "rate": "1mbit"}
            self.write_epoch(epoch_dir, 0, 0, links_add=[link])
            self.write_epoch(epoch_dir, 1, 10, links_add=[link])
            with self.assertRaisesRegex(ValueError, "duplicate add"):
                generator.derive_contacts_from_epochs(
                    self.registry,
                    epoch_dir,
                    "NetSatBench-epoch*.json",
                    epoch_duration=10,
                )

    def test_registry_rejects_invalid_udp_address_and_port(self):
        registry = {
            **self.registry,
            "udp_port": 70000,
            "contacts": [["sat1", "sat2"]],
        }
        with self.assertRaisesRegex(ValueError, "udp_port"):
            generator.validate_ion_node_registry(registry)

        registry["udp_port"] = 1113
        registry["nodes"] = {
            **self.registry["nodes"],
            "sat2": {
                **self.registry["nodes"]["sat2"],
                "udp_cla_address": "not-an-ip",
            },
        }
        with self.assertRaisesRegex(ValueError, "invalid udp_cla_address"):
            generator.validate_ion_node_registry(registry)

    def test_registry_rejects_a_path_traversal_node_name(self):
        nodes = dict(self.registry["nodes"])
        nodes["../sat2"] = nodes.pop("sat2")
        registry = {
            **self.registry,
            "nodes": nodes,
            "contacts": [["sat1", "../sat2"]],
        }
        with self.assertRaisesRegex(ValueError, "invalid node name"):
            generator.validate_ion_node_registry(registry)


if __name__ == "__main__":
    unittest.main()
