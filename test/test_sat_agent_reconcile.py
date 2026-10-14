import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).parents[1]


def load_sat_agent():
    sys.modules.setdefault("etcd3", types.SimpleNamespace())
    spec = importlib.util.spec_from_file_location(
        "sat_agent_under_test",
        ROOT / "sat-container/sat-agent.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LinkReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.agent = load_sat_agent()
        self.agent.node_name = "sat2"
        self.link = json.dumps({
            "endpoint1": "sat2",
            "endpoint2": "sat3",
            "vni": 42,
            "rate": "1mbit",
        }).encode()
        self.metadata = types.SimpleNamespace(key=b"/config/links/sat2/vl_sat3_1")
        self.etcd = types.SimpleNamespace(
            get_prefix=lambda _prefix: [(self.link, self.metadata)]
        )

    def test_periodic_reconcile_does_not_refresh_an_existing_link(self):
        with (
            patch.object(self.agent, "get_node_overlay_ip", return_value="172.100.0.10"),
            patch.object(self.agent, "link_operational", return_value=True),
            patch.object(self.agent.threading, "Thread") as thread,
        ):
            self.agent.reconcile_links(
                self.etcd,
                remove_stale=False,
                refresh_existing=False,
            )

        thread.assert_not_called()

    def test_periodic_reconcile_repairs_an_unhealthy_existing_link(self):
        with (
            patch.object(self.agent, "get_node_overlay_ip", return_value="172.100.0.10"),
            patch.object(self.agent, "link_operational", return_value=False),
            patch.object(
                self.agent,
                "get_remote_ip",
                side_effect=("10.0.0.2", "10.0.0.3"),
            ),
            patch.object(self.agent.threading, "Thread") as thread,
        ):
            self.agent.reconcile_links(
                self.etcd,
                remove_stale=False,
                refresh_existing=False,
            )

        thread.assert_called_once()

    def test_full_reconcile_refreshes_an_existing_link(self):
        with (
            patch.object(self.agent, "link_exists", return_value=True),
            patch.object(
                self.agent,
                "get_remote_ip",
                side_effect=("10.0.0.2", "10.0.0.3"),
            ),
            patch.object(
                self.agent,
                "get_node_overlay_ip",
                return_value="172.100.0.10",
            ),
            patch.object(self.agent.threading, "Thread") as thread,
        ):
            self.agent.reconcile_links(
                self.etcd,
                remove_stale=False,
                refresh_existing=True,
            )

        thread.assert_called_once()

    def test_transient_apply_failure_does_not_delete_a_desired_link(self):
        with (
            patch.object(self.agent, "apply_link_value", return_value=None),
            patch.object(
                self.agent,
                "list_vxlan_links",
                return_value={"vl_sat3_1"},
            ),
            patch.object(self.agent.threading, "Thread") as thread,
        ):
            self.agent.reconcile_links(
                self.etcd,
                remove_stale=True,
                refresh_existing=False,
            )

        thread.assert_not_called()

    def test_existing_link_repair_restores_mtu_state_address_and_route(self):
        self.agent.vxlan_link_mtu = 1408
        self.agent.my_config = {"L3-config": {"cidr": "172.100.0.0/24"}}
        self.agent.l3_flags = {"enable-netem": False, "enable-routing": False}

        with (
            patch.object(self.agent, "link_exists", return_value=True),
            patch.object(self.agent, "run") as run,
            patch.object(self.agent, "ensure_direct_neighbor_route") as ensure_route,
        ):
            self.agent.create_vxlan_link(
                "vl_sat3_1",
                "42",
                "10.0.0.3",
                "10.0.0.2",
                peer_overlay_ip="172.100.0.10",
            )

        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(["ip", "link", "set", "vl_sat3_1", "mtu", "1408"], commands)
        self.assertIn(["ip", "link", "set", "dev", "vl_sat3_1", "up"], commands)
        self.assertIn(
            ["ip", "addr", "replace", "172.100.0.254/32", "dev", "vl_sat3_1"],
            commands,
        )
        ensure_route.assert_called_once_with("vl_sat3_1", "172.100.0.10")

    def test_link_operational_checks_up_route_and_netem(self):
        self.agent.my_config = {}
        self.agent.l3_flags = {"enable-netem": True}
        completed = self.agent.subprocess.CompletedProcess
        responses = [
            completed([], 0, "7: vl_sat3_1: <BROADCAST,MULTICAST,UP,LOWER_UP>", ""),
            completed([], 0, "172.100.0.10 dev vl_sat3_1 src 172.100.0.254", ""),
            completed([], 0, "qdisc netem 1: root refcnt 2 delay 1s", ""),
        ]

        with patch.object(self.agent, "run", side_effect=responses):
            healthy = self.agent.link_operational(
                "vl_sat3_1",
                peer_overlay_ip="172.100.0.10",
                netem_opts={"delay": "1s"},
            )

        self.assertTrue(healthy)

    def test_link_operational_rejects_stale_netem_parameters(self):
        self.agent.my_config = {}
        self.agent.l3_flags = {"enable-netem": True}
        completed = self.agent.subprocess.CompletedProcess
        responses = [
            completed([], 0, "7: vl_sat3_1: <BROADCAST,MULTICAST,UP,LOWER_UP>", ""),
            completed([], 0, "172.100.0.10 dev vl_sat3_1 src 172.100.0.254", ""),
            completed(
                [],
                0,
                "qdisc netem 1: root limit 1000 delay 40ms loss 0% rate 100Mbit",
                "",
            ),
        ]

        with patch.object(self.agent, "run", side_effect=responses):
            healthy = self.agent.link_operational(
                "vl_sat3_1",
                peer_overlay_ip="172.100.0.10",
                netem_opts={
                    "delay": "4ms",
                    "loss": "0",
                    "rate": "100mbit",
                    "limit": 1000,
                },
            )

        self.assertFalse(healthy)

    def test_netem_comparison_normalizes_units(self):
        self.assertTrue(
            self.agent.netem_matches(
                "qdisc netem 1: root limit 1200 delay 0.004s loss random 1% rate 0.1Gbit",
                {"limit": "1200", "delay": "4ms", "loss": 1, "rate": "100mbit"},
            )
        )

    def test_link_operational_rejects_a_down_interface(self):
        self.agent.my_config = {}
        completed = self.agent.subprocess.CompletedProcess
        with patch.object(
            self.agent,
            "run",
            return_value=completed([], 0, "7: vl_sat3_1: <BROADCAST,MULTICAST>", ""),
        ):
            self.assertFalse(self.agent.link_operational("vl_sat3_1"))


if __name__ == "__main__":
    unittest.main()
