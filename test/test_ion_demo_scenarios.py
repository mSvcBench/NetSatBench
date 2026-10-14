import importlib.util
import json
import unittest
from fractions import Fraction
from pathlib import Path


ROOT = Path(__file__).parents[1]
SCENARIO_ROOT = ROOT / "examples/10nodes-ion-dtn/epochs"
EXPECTED_NODES = {"usr1", "grd1", *(f"sat{index}" for index in range(1, 9))}
DEMO_SCENARIO_NAMES = (
    "00-all-open",
    "01-store-carry-forward",
    "02-cloudy-ground",
    "03-alternate-path",
    "04-multi-bundle-queue",
    "05-short-contact-capacity",
    "06-handover-overlap",
)


def load_module(module_name, relative_path):
    """Load CLI scripts whose hyphenated filenames are not importable modules."""
    module_spec = importlib.util.spec_from_file_location(
        module_name, ROOT / relative_path
    )
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


generator = load_module(
    "nsb_ion_generate_configs", "utils/nsb-ion-generate-configs.py"
)
scenario_generator = load_module(
    "nsb_ion_generate_demo_scenarios",
    "utils/nsb-ion-generate-demo-scenarios.py",
)


class IonDemoScenarioTests(unittest.TestCase):
    def test_subsecond_image_enables_ten_millisecond_rfxclock_polling(self):
        config = json.loads(
            (ROOT / "examples/10nodes-ion-dtn/sat-config.json").read_text()
        )
        common_rules = config["node-config-common"]
        self.assertEqual(len(common_rules), 3)
        for rule in common_rules:
            common = rule["config-common"]
            self.assertEqual(common["env"]["ION_RFXCLOCK_POLL_MSEC"], "10")

    def setUp(self):
        self.registry = generator.load_ion_node_registry(
            ROOT / "examples/10nodes-ion-dtn/ion/ion-node-registry.json"
        )

    def test_satellite_metadata_describes_one_complete_eight_node_orbit(self):
        node_config = json.loads(
            (
                ROOT / "examples/10nodes-ion-dtn/sat-config.json"
            ).read_text(encoding="utf-8")
        )
        satellites = [
            node_config["nodes"][f"sat{index}"] for index in range(1, 9)
        ]

        self.assertEqual(
            [
                satellite["metadata"]["sat_config"]["sat_orbit"]
                for satellite in satellites
            ],
            [1] * 8,
        )
        self.assertEqual(
            [
                satellite["metadata"]["sat_config"]["sat_num_in_orbit"]
                for satellite in satellites
            ],
            list(range(1, 9)),
        )
        self.assertTrue(
            all(
                satellite["metadata"]["shell_config"]["number_of_orbit"] == 1
                and satellite["metadata"]["shell_config"][
                    "number_of_satellite_per_orbit"
                ]
                == 8
                for satellite in satellites
            )
        )
        self.assertEqual(node_config["nodes"]["sat1"]["n_antennas"], 3)
        self.assertEqual(node_config["nodes"]["sat5"]["n_antennas"], 3)

    def test_udp_cla_addresses_are_derived_from_netsatbench_config(self):
        raw_registry = json.loads(
            (ROOT / "examples/10nodes-ion-dtn/ion/ion-node-registry.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(
            all(
                "udp_cla_address" not in node
                for node in raw_registry["nodes"].values()
            )
        )
        self.assertEqual(
            self.registry["nodes"]["usr1"]["udp_cla_address"], "172.102.0.2"
        )
        self.assertEqual(
            self.registry["nodes"]["sat1"]["udp_cla_address"], "172.100.0.2"
        )
        self.assertEqual(
            self.registry["nodes"]["sat8"]["udp_cla_address"], "172.100.0.30"
        )
        self.assertEqual(
            self.registry["nodes"]["grd1"]["udp_cla_address"], "172.101.0.2"
        )

    def epoch_paths(self, scenario_name):
        return sorted(
            (SCENARIO_ROOT / scenario_name).glob("NetSatBench-epoch*.json"),
            key=generator.epoch_sort_key,
        )

    def scenario_nodes(self, scenario_name):
        referenced_nodes = set()
        for epoch_path in self.epoch_paths(scenario_name):
            epoch_data = json.loads(epoch_path.read_text(encoding="utf-8"))
            for action in ("links-add", "links-update", "links-del"):
                for link_descriptor in epoch_data.get(action, []):
                    referenced_nodes.update(
                        (
                            link_descriptor["endpoint1"],
                            link_descriptor["endpoint2"],
                        )
                    )
        return referenced_nodes

    def test_every_demo_scenario_uses_the_full_10_node_registry(self):
        for scenario_name in DEMO_SCENARIO_NAMES:
            with self.subTest(scenario=scenario_name):
                self.assertEqual(
                    self.scenario_nodes(scenario_name), EXPECTED_NODES
                )

    def test_checked_in_epochs_match_the_declarative_scenario_suite(self):
        for scenario_name, expected_epochs in (
            scenario_generator.build_demo_scenarios().items()
        ):
            epoch_paths = self.epoch_paths(scenario_name)
            self.assertEqual(len(epoch_paths), len(expected_epochs), scenario_name)
            for epoch_path, expected_epoch in zip(epoch_paths, expected_epochs):
                with self.subTest(scenario=scenario_name, epoch=epoch_path.name):
                    actual_epoch = json.loads(
                        epoch_path.read_text(encoding="utf-8")
                    )
                    self.assertEqual(actual_epoch, expected_epoch)

    def test_epoch_delays_quantize_to_registry_owlt(self):
        policy = self.registry.get("ion_subsecond_owlt_policy", "ceil-positive")
        for scenario_name in DEMO_SCENARIO_NAMES:
            for epoch_path in self.epoch_paths(scenario_name):
                epoch_data = json.loads(epoch_path.read_text(encoding="utf-8"))
                for action in ("links-add", "links-update", "links-del"):
                    for link_descriptor in epoch_data.get(action, []):
                        delay = link_descriptor.get("delay")
                        self.assertIsNotNone(
                            delay,
                            f"{scenario_name}/{epoch_path.name}/{action}",
                        )
                        self.assertEqual(
                            generator.resolve_contact_owlt_seconds(
                                self.registry,
                                link_descriptor,
                            ),
                            generator.quantize_ion_owlt_seconds(
                                generator.parse_delay_seconds(delay), policy
                            ),
                            f"{scenario_name}/{epoch_path.name}/{action}",
                        )

    def test_scenario_check_accepts_checked_in_epochs(self):
        scenario_generator.check_scenarios()

    def test_every_demo_scenario_generates_valid_rate_aware_contacts(self):
        for scenario_name in DEMO_SCENARIO_NAMES:
            with self.subTest(scenario=scenario_name):
                contacts = generator.derive_contacts_from_epochs(
                    self.registry,
                    SCENARIO_ROOT / scenario_name,
                    "NetSatBench-epoch*.json",
                    epoch_duration=(
                        10 if scenario_name == "05-short-contact-capacity" else 45
                    ),
                    contact_start_guard=4,
                    contact_end_guard=1,
                )
                self.assertTrue(contacts)
                self.assertTrue(
                    all(contact["rate_bytes_per_second"] > 0 for contact in contacts)
                )
                rendered = generator.render_global_ionrc(
                    {**self.registry, "contacts": contacts}
                )
                self.assertEqual(
                    rendered.count("a contact "),
                    len(contacts) * 2,
                )

    def test_capacity_scenario_has_expected_bottleneck_timeline(self):
        contacts = generator.derive_contacts_from_epochs(
            self.registry,
            SCENARIO_ROOT / "05-short-contact-capacity",
            "NetSatBench-epoch*.json",
            epoch_duration=10,
            contact_start_guard=4,
            contact_end_guard=1,
        )
        bottleneck = [
            contact
            for contact in contacts
            if {contact["from"], contact["to"]} == {"sat4", "sat5"}
        ]
        self.assertEqual(
            [
                (
                    contact["start"],
                    contact["end"],
                    contact["rate_bytes_per_second"],
                )
                for contact in bottleneck
            ],
            [
                (4, 10, 1_250_000),
                (10, 20, 125_000),
                (20, 30, 12_500),
                (30, 39, 1_250_000),
            ],
        )

    def test_store_carry_handovers_leave_exactly_one_second_ion_overlap(self):
        contacts = generator.derive_contacts_from_epochs(
            self.registry,
            SCENARIO_ROOT / "01-store-carry-forward",
            "NetSatBench-epoch*.json",
            epoch_duration=45,
            contact_start_guard=4,
            contact_end_guard=1,
        )

        def window(left, right):
            return next(
                (contact["start"], contact["end"])
                for contact in contacts
                if {contact["from"], contact["to"]} == {left, right}
            )

        chain = ("usr1", "sat1", "sat2", "sat3", "sat4", "sat5",
                 "sat6", "sat7", "sat8", "grd1")
        windows = [window(left, right) for left, right in zip(chain, chain[1:])]
        overlaps = [
            min(old[1], new[1]) - max(old[0], new[0])
            for old, new in zip(windows, windows[1:])
        ]
        self.assertEqual(overlaps, [1] * 8)

    def test_alternate_path_scenario_uses_one_orbital_ring(self):
        epoch_paths = self.epoch_paths("03-alternate-path")
        self.assertEqual(len(epoch_paths), 10)
        epoch = json.loads(epoch_paths[0].read_text(encoding="utf-8"))
        satellite_links = {
            frozenset((link["endpoint1"], link["endpoint2"]))
            for link in epoch["links-add"]
            if link["endpoint1"].startswith("sat")
            and link["endpoint2"].startswith("sat")
        }
        expected_ring = {
            frozenset((f"sat{index}", f"sat{index + 1}"))
            for index in range(1, 8)
        }
        expected_ring.add(frozenset(("sat8", "sat1")))

        self.assertEqual(satellite_links, expected_ring)
        self.assertIn(
            frozenset(("usr1", "sat1")),
            {
                frozenset((link["endpoint1"], link["endpoint2"]))
                for link in epoch["links-add"]
            },
        )
        self.assertIn(
            frozenset(("sat5", "grd1")),
            {
                frozenset((link["endpoint1"], link["endpoint2"]))
                for link in epoch["links-add"]
            },
        )

        ring_rates = {
            frozenset((link["endpoint1"], link["endpoint2"])): link["rate"]
            for link in epoch["links-add"]
            if link["endpoint1"].startswith("sat")
            and link["endpoint2"].startswith("sat")
        }
        for edge in (
            ("sat1", "sat8"),
            ("sat8", "sat7"),
            ("sat7", "sat6"),
            ("sat6", "sat5"),
        ):
            self.assertEqual(ring_rates[frozenset(edge)], "5mbit")

        for epoch_path in epoch_paths[1:]:
            later_epoch = json.loads(epoch_path.read_text(encoding="utf-8"))
            changed_links = [
                link
                for action in ("links-add", "links-update", "links-del")
                for link in later_epoch[action]
            ]
            self.assertTrue(changed_links)
            self.assertTrue(
                all(
                    {link["endpoint1"], link["endpoint2"]}
                    == {"sat2", "sat3"}
                    for link in changed_links
                )
            )

    def test_alternate_path_scenario_has_expected_primary_link_timeline(self):
        contacts = generator.derive_contacts_from_epochs(
            self.registry,
            SCENARIO_ROOT / "03-alternate-path",
            "NetSatBench-epoch*.json",
            epoch_duration=45,
            contact_start_guard=4,
            contact_end_guard=1,
        )
        primary_link = [
            contact
            for contact in contacts
            if {contact["from"], contact["to"]} == {"sat2", "sat3"}
        ]
        self.assertEqual(
            [
                (
                    contact["start"],
                    contact["end"],
                    contact["rate_bytes_per_second"],
                )
                for contact in primary_link
            ],
            [
                (4, 45, 1_250_000),
                (45, 90, 375_000),
                (90, 135, 125_000),
                (135, 179, 12_500),
                (229, 270, 12_500),
                (270, 315, 125_000),
                (315, 360, 375_000),
                (360, 449, 1_250_000),
            ],
        )
        constrained_volumes = [
            (contact["end"] - contact["start"])
            * contact["rate_bytes_per_second"]
            for contact in primary_link
            if contact["rate_bytes_per_second"] == 12_500
        ]
        self.assertEqual(constrained_volumes, [550_000, 512_500])
        self.assertTrue(
            all(volume < 1024 * 1024 for volume in constrained_volumes)
        )

    def test_alternate_path_rates_bracket_end_to_end_crossover(self):
        counterclockwise_cost = Fraction(2, 10) + Fraction(4, 5)

        def clockwise_cost(variable_rate_mbps):
            return Fraction(5, 10) + Fraction(1, variable_rate_mbps)

        self.assertEqual(clockwise_cost(2), counterclockwise_cost)
        self.assertLess(clockwise_cost(3), counterclockwise_cost)
        self.assertGreater(clockwise_cost(1), counterclockwise_cost)

    def test_multi_bundle_queue_has_two_constrained_service_windows(self):
        contacts = generator.derive_contacts_from_epochs(
            self.registry,
            SCENARIO_ROOT / "04-multi-bundle-queue",
            "NetSatBench-epoch*.json",
            epoch_duration=45,
            contact_start_guard=4,
            contact_end_guard=1,
        )
        bottleneck = [
            contact
            for contact in contacts
            if {contact["from"], contact["to"]} == {"sat4", "sat5"}
        ]
        self.assertEqual(
            [
                (
                    contact["start"],
                    contact["end"],
                    contact["rate_bytes_per_second"],
                )
                for contact in bottleneck
            ],
            [
                (94, 134, 250_000),
                (192, 232, 125_000),
                (282, 629, 250_000),
            ],
        )
        self.assertEqual(
            [
                (contact["end"] - contact["start"])
                * contact["rate_bytes_per_second"]
                for contact in bottleneck[:2]
            ],
            [10_000_000, 5_000_000],
        )

    def test_handover_overlap_preserves_native_addition_clock_in_61_epochs(self):
        epochs = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in self.epoch_paths("06-handover-overlap")
        ]
        self.assertEqual(len(epochs), 61)
        timestamps = [
            scenario_generator.datetime.fromisoformat(
                epoch["time"].replace("Z", "+00:00")
            )
            for epoch in epochs
        ]
        offsets = [
            int((timestamp - timestamps[0]).total_seconds())
            for timestamp in timestamps
        ]
        expected = [0, 2, 4, 7, 8, 13, 30, 35, 37]
        for cycle in range(1, 7):
            expected.extend(
                cycle * 60 + offset
                for offset in (2, 4, 7, 8, 13, 30, 35, 37)
            )
        expected.extend(7 * 60 + offset for offset in (2, 4, 7, 9))
        self.assertEqual(offsets, expected)

    def test_handover_guards_leave_one_second_access_overlap(self):
        contacts = generator.derive_contacts_from_epochs(
            self.registry,
            SCENARIO_ROOT / "06-handover-overlap",
            "NetSatBench-epoch*.json",
            epoch_duration=45,
            contact_start_guard=4,
            contact_end_guard=1,
        )

        def window(left, right):
            return next(
                (contact["start"], contact["end"])
                for contact in contacts
                if {contact["from"], contact["to"]} == {left, right}
            )

        user_sat2 = window("usr1", "sat2")
        user_sat3 = window("usr1", "sat3")
        ground_sat8 = window("grd1", "sat8")
        ground_sat1 = window("grd1", "sat1")
        self.assertEqual(user_sat2, (6, 67))
        self.assertEqual(user_sat3, (66, 127))
        self.assertEqual(ground_sat8, (11, 72))
        self.assertEqual(ground_sat1, (71, 132))
        self.assertEqual(
            min(user_sat2[1], user_sat3[1])
            - max(user_sat2[0], user_sat3[0]),
            1,
        )
        self.assertEqual(
            min(ground_sat8[1], ground_sat1[1])
            - max(ground_sat8[0], ground_sat1[0]),
            1,
        )


if __name__ == "__main__":
    unittest.main()
