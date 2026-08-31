#!/usr/bin/env python3

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import logging
import math
import re
from pathlib import Path

logging.basicConfig(level="INFO", format="[%(levelname)s] %(message)s")
log = logging.getLogger("nsb-ion-generate-configs")

DEFAULT_CONTACT_DURATION_SECONDS = 600
DEFAULT_SDR_HEAP_WORDS = 20_000_000
UDP_CLA_MAX_PAYLOAD_BYTES = 1200
NODE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

BIT_RATE_UNIT_MULTIPLIERS = {
    "bit": 1,
    "kbit": 1_000,
    "mbit": 1_000_000,
    "gbit": 1_000_000_000,
    "tbit": 1_000_000_000_000,
    "bps": 1,
    "kbps": 1_000,
    "mbps": 1_000_000,
    "gbps": 1_000_000_000,
    "tbps": 1_000_000_000_000,
}


@dataclass
class ActiveContactWindow:
    """Physical link window later rendered as two directed ION contacts."""

    endpoint1: str
    endpoint2: str
    start_seconds: int
    rate_bytes_per_second: int
    ion_owlt_seconds: int
    source_epoch: str


def load_ion_node_registry(registry_path):
    """Load the ION registry and derive UDP CLA addresses from NetSatBench."""
    with open(registry_path, "r", encoding="utf-8") as registry_file:
        ion_registry = json.load(registry_file)
    populate_udp_cla_addresses(ion_registry, Path(registry_path))
    return ion_registry


def resolve_netsatbench_node_configs(config_path):
    """Resolve the IPv4 CIDRs produced by NetSatBench auto-assignment rules."""
    with open(config_path, "r", encoding="utf-8") as config_file:
        netsatbench_config = json.load(config_file)

    common_config_rules = netsatbench_config.get("node-config-common", [])
    if isinstance(common_config_rules, dict):
        common_config_rules = [
            {
                "match-key": "any",
                "match-value": True,
                "config-common": common_config_rules,
            }
        ]

    # Counters are independent per common-config CIDR rule, matching nsb-init.
    next_subnet_index = {}
    resolved_node_configs = {}
    for node_name, node_config in netsatbench_config.get("nodes", {}).items():
        node_l3_config = node_config.get("L3-config", {})
        assigned_cidr = node_l3_config.get("cidr")

        for common_rule_index, common_rule in enumerate(common_config_rules):
            if not node_matches_config_rule(node_config, common_rule):
                continue
            common_l3_config = common_rule.get("config-common", {}).get(
                "L3-config", {}
            )
            if assigned_cidr or not common_l3_config.get("auto-assign-ips"):
                break
            for cidr_rule_index, cidr_rule in enumerate(
                common_l3_config.get("auto-assign-super-cidr", [])
            ):
                if not node_matches_config_rule(node_config, cidr_rule):
                    continue
                super_cidr = cidr_rule.get("super-cidr")
                if not super_cidr:
                    continue
                counter_key = (common_rule_index, cidr_rule_index)
                subnet_index = next_subnet_index.get(counter_key, 0)
                assigned_cidr = allocate_indexed_ipv4_subnet(
                    super_cidr, subnet_index, prefix_length=30
                )
                next_subnet_index[counter_key] = subnet_index + 1
                break
            break

        resolved_node_configs[node_name] = {
            **node_config,
            "L3-config": {**node_l3_config, "cidr": assigned_cidr},
        }

    return resolved_node_configs


def get_nested_config_value(data, dotted_key):
    value = data
    for key in dotted_key.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def node_matches_config_rule(node_config, config_rule):
    if config_rule.get("match-key") == "any":
        return True
    return get_nested_config_value(
        node_config, config_rule.get("match-key", "")
    ) == config_rule.get(
        "match-value",
    )


def allocate_indexed_ipv4_subnet(super_cidr, subnet_index, prefix_length):
    """Select a subnet by index without materializing the entire parent range."""
    parent_network = ipaddress.ip_network(super_cidr)
    if parent_network.version != 4:
        raise ValueError(f"expected IPv4 super CIDR, got {super_cidr!r}")
    if prefix_length < parent_network.prefixlen:
        raise ValueError(f"cannot allocate /{prefix_length} from {super_cidr}")
    subnet_size = 1 << (32 - prefix_length)
    subnet_address = int(parent_network.network_address) + (
        subnet_index * subnet_size
    )
    allocated_subnet = ipaddress.ip_network((subnet_address, prefix_length))
    if not allocated_subnet.subnet_of(parent_network):
        raise ValueError(f"subnet index {subnet_index} exceeds {super_cidr}")
    return str(allocated_subnet)


def select_udp_cla_ipv4_address(cidr):
    """Select the address used by ION's UDP convergence-layer adapter."""
    network = ipaddress.ip_network(cidr, strict=False)
    if network.version != 4:
        raise ValueError(f"ION UDP overlay requires an IPv4 CIDR, got {cidr!r}")
    if network.num_addresses == 1:
        return str(network.network_address)
    if network.prefixlen <= 30:
        return str(network.broadcast_address - 1)
    return str(network.network_address + (network.num_addresses - 1))


def populate_udp_cla_addresses(ion_registry, registry_path):
    """Populate missing UDP CLA addresses from the NetSatBench node config."""
    nodes_without_udp_cla_address = [
        node_name
        for node_name, node_config in ion_registry.get("nodes", {}).items()
        if not node_config.get("udp_cla_address")
    ]
    if not nodes_without_udp_cla_address:
        return ion_registry

    node_config_reference = ion_registry.get("netsatbench_node_config")
    if not node_config_reference:
        raise ValueError(
            "nodes are missing udp_cla_address and the ION registry has no "
            "netsatbench_node_config source: "
            + ", ".join(nodes_without_udp_cla_address)
        )

    node_config_path = Path(node_config_reference)
    if not node_config_path.is_absolute():
        node_config_path = Path(registry_path).resolve().parent / node_config_path
    node_config_path = node_config_path.resolve()
    if not node_config_path.is_file():
        raise ValueError(f"NetSatBench node config not found: {node_config_path}")

    netsatbench_nodes = resolve_netsatbench_node_configs(node_config_path)
    for node_name in nodes_without_udp_cla_address:
        netsatbench_node = netsatbench_nodes.get(node_name)
        if netsatbench_node is None:
            raise ValueError(
                f"ION node {node_name!r} is missing from {node_config_path}"
            )
        node_cidr = netsatbench_node.get("L3-config", {}).get("cidr")
        if not node_cidr:
            raise ValueError(
                f"ION node {node_name!r} has no IPv4 CIDR in {node_config_path}"
            )
        ion_registry["nodes"][node_name]["udp_cla_address"] = (
            select_udp_cla_ipv4_address(node_cidr)
        )

    return ion_registry


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_utc_datetime(value):
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(
            f"invalid UTC timestamp {value!r}; expected ISO-8601 like "
            "2026-07-20T10:30:00Z"
        ) from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def format_ion_time(value):
    return value.astimezone(timezone.utc).strftime("%Y/%m/%d-%H:%M:%S")


def epoch_sort_key(path):
    match = re.search(r"epoch(\d+)", path.name)
    if match:
        return int(match.group(1)), path.name

    return float("inf"), path.name


def epoch_timestamp(epoch, epoch_path):
    epoch_time = epoch.get("epoch-time")
    if epoch_time not in (None, ""):
        return float(epoch_time)

    wall_time = epoch.get("time")
    if wall_time:
        return parse_utc_datetime(wall_time).timestamp()

    raise ValueError(f"epoch file is missing time or epoch-time: {epoch_path}")


def epoch_offset_seconds(timestamp, first_timestamp, epoch_path):
    offset = timestamp - first_timestamp
    rounded = round(offset)
    if rounded < 0:
        raise ValueError(f"epoch time moves backwards: {epoch_path}")
    if abs(offset - rounded) > 0.001:
        raise ValueError(
            f"epoch offset is not an integer number of seconds: {epoch_path}"
        )

    return int(rounded)


def link_nodes(link):
    return link["endpoint1"], link["endpoint2"]


def link_key(left, right):
    return tuple(sorted((left, right)))


def parse_rate_bytes_per_second(value):
    """Convert NetSatBench/tc bit-rate values to ION's byte-per-second rate."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        bits_per_second = float(value)
    elif isinstance(value, str):
        match = re.fullmatch(
            r"\s*(\d+(?:\.\d+)?)\s*([kmgt]?(?:bit|bps))\s*",
            value,
            re.IGNORECASE,
        )
        if not match:
            raise ValueError(
                f"invalid link rate {value!r}; expected values like "
                "100kbit, 10mbit, or an unquoted numeric bit rate"
            )
        amount, unit = match.groups()
        bits_per_second = float(amount) * BIT_RATE_UNIT_MULTIPLIERS[unit.lower()]
    else:
        raise ValueError(f"invalid link rate type: {type(value).__name__}")

    if bits_per_second <= 0:
        raise ValueError(f"link rate must be positive: {value!r}")

    # ION accepts an integer byte-per-second rate. Round up so a positive
    # NetSatBench rate never becomes a zero-capacity contact.
    return max(1, int((bits_per_second + 7) // 8))


def resolve_contact_rate_bytes_per_second(registry, link_descriptor):
    """Resolve BP payload service rate independently from physical wire rate.

    ``rate`` remains the NetSatBench/NetEm wire rate.  Scenarios that need to
    account for UDP/VXLAN encapsulation can additionally declare ``ion-rate``;
    ION then budgets BP bytes at that rate while NetEm continues to shape the
    larger on-wire frames at ``rate``.  Existing epochs retain their original
    one-rate behaviour when ``ion-rate`` is absent.
    """
    link_rate = link_descriptor.get("ion-rate", link_descriptor.get("rate"))
    if link_rate in (None, ""):
        return int(registry["default_contact_rate_bytes_per_second"])
    return parse_rate_bytes_per_second(link_rate)


def parse_delay_seconds(value):
    """Convert a NetSatBench/tc delay value to seconds."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        delay_seconds = float(value)
    elif isinstance(value, str):
        match = re.fullmatch(
            r"\s*(\d+(?:\.\d+)?)\s*(us|ms|s)\s*",
            value,
            re.IGNORECASE,
        )
        if not match:
            raise ValueError(
                f"invalid link delay {value!r}; expected values like "
                "500us, 4.7ms, or 1.2s"
            )
        amount, unit = match.groups()
        multiplier = {"us": 0.000001, "ms": 0.001, "s": 1.0}[unit.lower()]
        delay_seconds = float(amount) * multiplier
    else:
        raise ValueError(f"invalid link delay type: {type(value).__name__}")

    if not math.isfinite(delay_seconds) or delay_seconds <= 0:
        raise ValueError(f"link delay must be positive: {value!r}")

    return delay_seconds


def quantize_ion_owlt_seconds(delay_seconds, subsecond_policy="ceil-positive"):
    """Map physical delay to stock ION's whole-second OWLT representation."""
    if subsecond_policy not in {"ceil-positive", "zero"}:
        raise ValueError(
            "ion_subsecond_owlt_policy must be 'ceil-positive' or 'zero'"
        )
    if delay_seconds < 1.0 and subsecond_policy == "zero":
        # This is an explicit modelling policy, not mathematical rounding.
        # Linux/NetEm retains the physical subsecond propagation delay; only
        # stock ION's integer-second routing representation is set to zero.
        return 0
    return max(1, math.ceil(delay_seconds))


def resolve_contact_owlt_seconds(registry, link_descriptor):
    """Use epoch delay when present, otherwise use the registry OWLT fallback."""
    policy = registry.get("ion_subsecond_owlt_policy", "ceil-positive")
    link_delay = link_descriptor.get("delay")
    if link_delay in (None, ""):
        return quantize_ion_owlt_seconds(
            float(registry["owlt_seconds"]), policy
        )
    return quantize_ion_owlt_seconds(parse_delay_seconds(link_delay), policy)


def append_contact_segment(contacts, active_contact, end_seconds, source_end):
    """Close one active rate segment and append its generator-facing record."""
    if end_seconds <= active_contact.start_seconds:
        return
    contacts.append(
        {
            "from": active_contact.endpoint1,
            "to": active_contact.endpoint2,
            "start": active_contact.start_seconds,
            "end": end_seconds,
            "rate_bytes_per_second": active_contact.rate_bytes_per_second,
            "ion_owlt_seconds": active_contact.ion_owlt_seconds,
            "source_epoch": f"{active_contact.source_epoch}..{source_end}",
        }
    )


def derive_contacts_from_epochs(
    registry,
    epoch_dir,
    file_pattern,
    epoch_duration,
    contact_start_guard=0,
    contact_end_guard=0,
):
    """Derive ION contacts inside, rather than on, physical link boundaries."""
    if contact_start_guard < 0 or contact_end_guard < 0:
        raise ValueError("contact guards must be non-negative")

    epoch_paths = sorted(Path(epoch_dir).glob(file_pattern), key=epoch_sort_key)
    if not epoch_paths:
        raise ValueError(f"no epoch files matched {file_pattern!r} under {epoch_dir}")

    loaded_epochs = []
    for epoch_path in epoch_paths:
        epoch = load_json(epoch_path)
        loaded_epochs.append((epoch_path, epoch, epoch_timestamp(epoch, epoch_path)))

    first_timestamp = loaded_epochs[0][2]
    contacts = []
    active_links = {}
    nodes = registry["nodes"]

    for epoch_path, epoch, timestamp in loaded_epochs:
        start = epoch_offset_seconds(timestamp, first_timestamp, epoch_path)

        for action in ["links-add", "links-del", "links-update"]:
            for link in epoch.get(action, []):
                left, right = link_nodes(link)
                if left not in nodes or right not in nodes:
                    raise ValueError(
                        f"epoch link references node not in ION registry: "
                        f"{epoch_path}: {left} <-> {right}"
                    )

        for link in epoch.get("links-del", []):
            left, right = link_nodes(link)
            key = link_key(left, right)
            active_contact = active_links.pop(key, None)
            if active_contact is None:
                raise ValueError(
                    f"delete references inactive link in {epoch_path}: "
                    f"{left} <-> {right}"
                )

            guarded_end = start - contact_end_guard
            if guarded_end <= active_contact.start_seconds:
                raise ValueError(
                    "contact guards eliminate physical link window "
                    f"{left} <-> {right} ending in {epoch_path.name}"
                )
            append_contact_segment(
                contacts,
                active_contact,
                guarded_end,
                epoch_path.name,
            )

        for link in epoch.get("links-update", []):
            left, right = link_nodes(link)
            key = link_key(left, right)
            active_contact = active_links.get(key)
            if active_contact is None:
                raise ValueError(
                    f"update references inactive link in {epoch_path}: "
                    f"{left} <-> {right}"
                )

            updated_rate = (
                resolve_contact_rate_bytes_per_second(registry, link)
                if (
                    link.get("ion-rate") not in (None, "")
                    or link.get("rate") not in (None, "")
                )
                else active_contact.rate_bytes_per_second
            )
            updated_owlt = (
                resolve_contact_owlt_seconds(registry, link)
                if link.get("delay") not in (None, "")
                else active_contact.ion_owlt_seconds
            )
            if (
                updated_rate == active_contact.rate_bytes_per_second
                and updated_owlt == active_contact.ion_owlt_seconds
            ):
                continue

            if start <= active_contact.start_seconds:
                # The update precedes the guarded contact start, so it changes
                # the initial segment without creating an empty ION contact.
                active_links[key] = ActiveContactWindow(
                    endpoint1=active_contact.endpoint1,
                    endpoint2=active_contact.endpoint2,
                    start_seconds=active_contact.start_seconds,
                    rate_bytes_per_second=updated_rate,
                    ion_owlt_seconds=updated_owlt,
                    source_epoch=active_contact.source_epoch,
                )
                continue

            # A rate or whole-second OWLT change needs a new ION plan segment.
            append_contact_segment(
                contacts,
                active_contact,
                start,
                epoch_path.name,
            )
            active_links[key] = ActiveContactWindow(
                endpoint1=active_contact.endpoint1,
                endpoint2=active_contact.endpoint2,
                start_seconds=start,
                rate_bytes_per_second=updated_rate,
                ion_owlt_seconds=updated_owlt,
                source_epoch=epoch_path.name,
            )

        for link in epoch.get("links-add", []):
            left, right = link_nodes(link)
            key = link_key(left, right)
            if key in active_links:
                raise ValueError(
                    f"duplicate add for active link in {epoch_path}: "
                    f"{left} <-> {right}"
                )

            active_links[key] = ActiveContactWindow(
                endpoint1=left,
                endpoint2=right,
                start_seconds=start + contact_start_guard,
                rate_bytes_per_second=resolve_contact_rate_bytes_per_second(
                    registry, link
                ),
                ion_owlt_seconds=resolve_contact_owlt_seconds(registry, link),
                source_epoch=epoch_path.name,
            )

    scenario_end = (
        epoch_offset_seconds(loaded_epochs[-1][2], first_timestamp, loaded_epochs[-1][0])
        + epoch_duration
    )
    for active_contact in active_links.values():
        guarded_end = scenario_end - contact_end_guard
        if guarded_end <= active_contact.start_seconds:
            raise ValueError(
                "contact guards eliminate physical link window "
                f"{active_contact.endpoint1} <-> {active_contact.endpoint2} "
                "at scenario end"
            )
        append_contact_segment(
            contacts,
            active_contact,
            guarded_end,
            "end",
        )

    if not contacts:
        raise ValueError(f"no active contacts found under {epoch_dir}")

    return contacts


def ipn_node_number(registry, node_name):
    return int(registry["nodes"][node_name]["ion_node_number"])


def node_udp_cla_address(registry, node_name):
    return registry["nodes"][node_name]["udp_cla_address"]


def contact_nodes(contact):
    if isinstance(contact, dict):
        return contact["from"], contact["to"]

    return contact[0], contact[1]


def contact_window(registry, contact):
    if isinstance(contact, dict):
        return int(contact["start"]), int(contact["end"])

    return 0, int(registry["contact_duration_seconds"])


def render_contact_time(registry, offset_seconds):
    start_time = registry.get("_absolute_start_time")
    if start_time is None:
        return f"+{int(offset_seconds)}"

    return format_ion_time(start_time + timedelta(seconds=int(offset_seconds)))


def contact_source(contact):
    if isinstance(contact, dict):
        return contact.get("source_epoch")

    return None


def contact_segment_rate(registry, contact):
    if isinstance(contact, dict):
        return int(
            contact.get(
                "rate_bytes_per_second",
                registry["default_contact_rate_bytes_per_second"],
            )
        )
    return int(registry["default_contact_rate_bytes_per_second"])


def contact_segment_owlt(registry, contact):
    if isinstance(contact, dict):
        return int(
            contact.get(
                "ion_owlt_seconds",
                quantize_ion_owlt_seconds(
                    float(registry["owlt_seconds"]),
                    registry.get("ion_subsecond_owlt_policy", "ceil-positive"),
                ),
            )
        )
    return quantize_ion_owlt_seconds(
        float(registry["owlt_seconds"]),
        registry.get("ion_subsecond_owlt_policy", "ceil-positive"),
    )


def validate_ion_node_registry(registry):
    required = [
        "experiment",
        "udp_port",
        "default_contact_rate_bytes_per_second",
        "owlt_seconds",
        "nodes",
    ]

    for key in required:
        if key not in registry:
            raise ValueError(f"missing required key: {key}")

    path = registry.get("path", [])
    nodes = registry["nodes"]
    contacts = registry.get("contacts")

    for name in path:
        if name not in nodes:
            raise ValueError(f"path node is not defined in nodes: {name}")

    for key in ["default_source", "default_destination"]:
        node_name = registry.get(key)
        if node_name and node_name not in nodes:
            raise ValueError(f"{key} node is not defined in nodes: {node_name}")

    if contacts is None:
        if len(path) < 2:
            raise ValueError(
                "contacts are required unless --epoch-dir derives them or path "
                "contains at least two nodes"
            )
        contacts = [[left, right] for left, right in zip(path, path[1:])]
        registry["contacts"] = contacts

    for contact in contacts:
        if isinstance(contact, dict):
            for key in ["from", "to", "start", "end"]:
                if key not in contact:
                    raise ValueError(f"contact is missing required key {key}: {contact}")
            if int(contact["start"]) < 0:
                raise ValueError(f"contact start must be non-negative: {contact}")
            if int(contact["end"]) <= int(contact["start"]):
                raise ValueError(f"contact end must be greater than start: {contact}")
            if int(contact.get("rate_bytes_per_second", 1)) <= 0:
                raise ValueError(f"contact rate must be positive: {contact}")
            if int(contact.get("ion_owlt_seconds", 0)) < 0:
                raise ValueError(f"contact OWLT must be non-negative: {contact}")
        elif not isinstance(contact, (list, tuple)) or len(contact) != 2:
            raise ValueError(f"contact must contain exactly two nodes: {contact}")

        left, right = contact_nodes(contact)
        if left == right:
            raise ValueError(f"contact endpoints must be different: {contact}")
        for name in (left, right):
            if name not in nodes:
                raise ValueError(f"contact node is not defined in nodes: {name}")

    if not 1 <= int(registry["udp_port"]) <= 65535:
        raise ValueError("udp_port must be inside [1, 65535]")

    for name, node in nodes.items():
        if not NODE_NAME_PATTERN.fullmatch(name):
            raise ValueError(f"invalid node name: {name!r}")
        if "ion_node_number" not in node or "udp_cla_address" not in node:
            raise ValueError(
                f"node {name} requires ion_node_number and udp_cla_address"
            )
        if int(node["ion_node_number"]) <= 0:
            raise ValueError(f"node {name} ion_node_number must be positive")
        try:
            ipaddress.ip_address(node["udp_cla_address"])
        except ValueError as error:
            raise ValueError(
                f"node {name} has invalid udp_cla_address: "
                f"{node['udp_cla_address']!r}"
            ) from error

    ipn_node_numbers = [int(node["ion_node_number"]) for node in nodes.values()]
    if len(ipn_node_numbers) != len(set(ipn_node_numbers)):
        raise ValueError("ion_node_number values must be unique")

    udp_cla_addresses = [node["udp_cla_address"] for node in nodes.values()]
    if len(udp_cla_addresses) != len(set(udp_cla_addresses)):
        raise ValueError("udp_cla_address values must be unique")

    for value_key in [
        "udp_port",
        "default_contact_rate_bytes_per_second",
        "owlt_seconds",
    ]:
        if float(registry[value_key]) <= 0:
            raise ValueError(f"{value_key} must be positive")

    subsecond_policy = registry.get(
        "ion_subsecond_owlt_policy", "ceil-positive"
    )
    if subsecond_policy not in {"ceil-positive", "zero"}:
        raise ValueError(
            "ion_subsecond_owlt_policy must be 'ceil-positive' or 'zero'"
        )

    if int(registry.get("contact_duration_seconds", DEFAULT_CONTACT_DURATION_SECONDS)) <= 0:
        raise ValueError("contact_duration_seconds must be positive when set")


def direct_neighbors(registry, node_name):
    neighbors = []
    for contact in registry["contacts"]:
        left, right = contact_nodes(contact)
        if left == node_name:
            neighbors.append(right)
        elif right == node_name:
            neighbors.append(left)

    return sorted(set(neighbors))


def render_global_ionrc(registry):
    source = registry.get("default_source", "?")
    destination = registry.get("default_destination", "?")
    path = registry.get("path")
    contacts = registry["contacts"]
    lines = [
        "# Generated ION contact/range plan for NetSatBench ION-DTN prototype.",
        "# Source: ion-node-registry.json",
        "#",
        f"# Experiment: {registry['experiment']}",
        (
            f"# Primary path: {' -> '.join(path)}"
            if path
            else f"# Default flow: {source} -> {destination}"
        ),
        "#",
        (
            f"# Absolute time base: {format_ion_time(registry['_absolute_start_time'])} UTC."
            if registry.get("_absolute_start_time")
            else "# Relative times below are anchored at ionadmin execution time."
        ),
        "# Regenerate before each clean run if ion-node-registry.json changes.",
        "",
        "# Ranges declare one-way light time between adjacent ION nodes.",
    ]

    for contact in contacts:
        left, right = contact_nodes(contact)
        start, end = contact_window(registry, contact)
        start_text = render_contact_time(registry, start)
        end_text = render_contact_time(registry, end)
        a = ipn_node_number(registry, left)
        b = ipn_node_number(registry, right)
        owlt_seconds = contact_segment_owlt(registry, contact)
        source = contact_source(contact)
        if source:
            lines.append(f"# {source}: {left} <-> {right}")
        lines.append(f"a range {start_text} {end_text} {a} {b} {owlt_seconds}")
        lines.append(f"a range {start_text} {end_text} {b} {a} {owlt_seconds}")

    lines.extend([
        "",
        "# Contacts declare planned transmission opportunities and byte-per-second rate.",
    ])

    for contact in contacts:
        left, right = contact_nodes(contact)
        start, end = contact_window(registry, contact)
        start_text = render_contact_time(registry, start)
        end_text = render_contact_time(registry, end)
        a = ipn_node_number(registry, left)
        b = ipn_node_number(registry, right)
        rate = contact_segment_rate(registry, contact)
        source = contact_source(contact)
        if source:
            lines.append(f"# {source}: {left} <-> {right}")
        lines.append(f"a contact {start_text} {end_text} {a} {b} {rate}")
        lines.append(f"a contact {start_text} {end_text} {b} {a} {rate}")

    return "\n".join(lines) + "\n"


def render_node_bprc(registry, node_name):
    ion_id = ipn_node_number(registry, node_name)
    udp_port = int(registry["udp_port"])
    neighbors = direct_neighbors(registry, node_name)

    lines = [
        f"# Generated BP configuration for NetSatBench node {node_name}.",
        f"# ION node: {ion_id}",
        "# Source: ion-node-registry.json",
        "",
        "1",
        "a scheme ipn 'ipnfw' 'ipnadminep'",
        f"a endpoint ipn:{ion_id}.0 x",
        f"a endpoint ipn:{ion_id}.1 q",
        "a protocol udp 1400 100",
        f"a induct udp 0.0.0.0:{udp_port} udpcli",
    ]

    for neighbor_name in neighbors:
        lines.append(
            f"a outduct udp {node_udp_cla_address(registry, neighbor_name)}:"
            f"{udp_port} udpclo {UDP_CLA_MAX_PAYLOAD_BYTES}"
        )

    lines.extend([
        "r 'ipnadmin node.ipnrc'",
        "s",
    ])

    return "\n".join(lines) + "\n"


def render_node_ipnrc(registry, node_name):
    udp_port = int(registry["udp_port"])

    lines = [
        f"# Generated IPN/CGR neighbor plan for NetSatBench node {node_name}.",
        f"# ION node: {ipn_node_number(registry, node_name)}",
        "# Source: ion-node-registry.json",
        "# CGR uses the contact graph plus these direct-neighbor plans.",
        "",
    ]

    neighbors = direct_neighbors(registry, node_name)
    for neighbor_name in neighbors:
        rates = [
            contact_segment_rate(registry, contact)
            for contact in registry["contacts"]
            if node_name in contact_nodes(contact)
            and neighbor_name in contact_nodes(contact)
        ]
        rate = max(rates, default=int(registry["default_contact_rate_bytes_per_second"]))
        lines.append(f"# Neighbor: {neighbor_name}")
        lines.append(
            f"a plan {ipn_node_number(registry, neighbor_name)} "
            f"udp/{node_udp_cla_address(registry, neighbor_name)}:{udp_port} {rate}"
        )

    return "\n".join(lines).rstrip() + "\n"


def render_node_ionconfig(registry, node_name):
    ion_id = ipn_node_number(registry, node_name)
    wm_key = 90000 + ion_id
    heap_words = int(registry.get("sdr_heap_words", DEFAULT_SDR_HEAP_WORDS))

    lines = [
        f"# Generated ION local node configuration for NetSatBench node {node_name}.",
        f"# ION node: {ion_id}",
        "# Source: ion-node-registry.json",
        "",
        f"wmKey {wm_key}",
        f"sdrName ion{ion_id}",
        "wmSize 5000000",
        "configFlags 1",
        f"heapWords {heap_words}",
        "pathName /tmp",
    ]

    return "\n".join(lines) + "\n"


def render_node_ionrc(registry, node_name):
    ion_id = ipn_node_number(registry, node_name)
    horizon = int(registry["contact_duration_seconds"])

    lines = [
        f"# Generated ION startup script for NetSatBench node {node_name}.",
        f"# ION node: {ion_id}",
        "# Source: ion-node-registry.json",
        "",
        f"1 {ion_id} node.ionconfig",
        "s",
        f"m horizon +{horizon}",
        "r 'ionadmin ../global.ionrc'",
    ]

    return "\n".join(lines) + "\n"


def render_node_ionsecrc(registry, node_name):
    return ""


def main():
    parser = argparse.ArgumentParser(
        description="Generate ION config files from a NetSatBench DTN node registry."
    )
    parser.add_argument(
        "--registry",
        "--spec",
        dest="registry_path",
        required=True,
        help="Path to ion-node-registry.json.",
    )
    parser.add_argument("--output", required=True, help="Output directory")
    parser.add_argument(
        "--epoch-dir",
        help=(
            "Optional NetSatBench epoch directory. If set, links-add, "
            "links-update, and links-del entries are replayed to derive "
            "rate-aware contact windows."
        ),
    )
    parser.add_argument(
        "--file-pattern",
        default="NetSatBench-epoch*.json",
        help="Epoch filename glob used with --epoch-dir.",
    )
    parser.add_argument(
        "--epoch-duration",
        type=int,
        help=(
            "Seconds used to close contacts that remain active after the last "
            "epoch. Contact start times come from epoch timestamps."
        ),
    )
    parser.add_argument(
        "--absolute-start-time",
        help=(
            "UTC experiment T0 for absolute ION contact times, for example "
            "2026-07-20T10:30:00Z. If omitted, relative +N times are used."
        ),
    )
    parser.add_argument(
        "--contact-start-guard",
        type=int,
        default=4,
        help=(
            "Seconds between a physical links-add event and the corresponding "
            "ION contact start (canonical default: 4)."
        ),
    )
    parser.add_argument(
        "--contact-end-guard",
        type=int,
        default=1,
        help=(
            "Seconds between an ION contact end and the physical links-del "
            "event or scenario end (canonical default: 1)."
        ),
    )
    args = parser.parse_args()

    registry_path = Path(args.registry_path)
    output_dir = Path(args.output)

    registry = load_ion_node_registry(registry_path)
    registry.setdefault("contact_duration_seconds", DEFAULT_CONTACT_DURATION_SECONDS)
    if args.absolute_start_time:
        registry["_absolute_start_time"] = parse_utc_datetime(args.absolute_start_time)

    if args.epoch_dir:
        if args.epoch_duration is None:
            raise ValueError("--epoch-duration is required when --epoch-dir is set")
        if args.epoch_duration <= 0:
            raise ValueError("--epoch-duration must be positive")
        if args.contact_start_guard < 0 or args.contact_end_guard < 0:
            raise ValueError("contact guards must be non-negative")
        if args.contact_start_guard + args.contact_end_guard >= args.epoch_duration:
            raise ValueError(
                "contact guards must leave a positive window within one epoch"
            )

        registry["contacts"] = derive_contacts_from_epochs(
            registry,
            args.epoch_dir,
            args.file_pattern,
            args.epoch_duration,
            args.contact_start_guard,
            args.contact_end_guard,
        )
        registry["contact_duration_seconds"] = max(
            int(registry.get("contact_duration_seconds", DEFAULT_CONTACT_DURATION_SECONDS)),
            max(contact["end"] for contact in registry["contacts"]) + args.epoch_duration,
        )

    validate_ion_node_registry(registry)

    output_dir.mkdir(parents=True, exist_ok=True)

    global_ionrc = render_global_ionrc(registry)
    (output_dir / "global.ionrc").write_text(global_ionrc, encoding="utf-8")
    log.info(f"📝 wrote {output_dir / 'global.ionrc'}")

    for node_name in sorted(registry["nodes"]):
        node_dir = output_dir / node_name
        node_dir.mkdir(parents=True, exist_ok=True)

        node_bprc = render_node_bprc(registry, node_name)
        (node_dir / "node.bprc").write_text(node_bprc, encoding="utf-8")
        log.info(f"📝 wrote {node_dir / 'node.bprc'}")

        node_ipnrc = render_node_ipnrc(registry, node_name)
        (node_dir / "node.ipnrc").write_text(node_ipnrc, encoding="utf-8")
        log.info(f"📝 wrote {node_dir / 'node.ipnrc'}")

        node_ionconfig = render_node_ionconfig(registry, node_name)
        (node_dir / "node.ionconfig").write_text(node_ionconfig, encoding="utf-8")
        log.info(f"📝 wrote {node_dir / 'node.ionconfig'}")

        node_ionrc = render_node_ionrc(registry, node_name)
        (node_dir / "node.ionrc").write_text(node_ionrc, encoding="utf-8")
        log.info(f"📝 wrote {node_dir / 'node.ionrc'}")

        node_ionsecrc = render_node_ionsecrc(registry, node_name)
        (node_dir / "node.ionsecrc").write_text(node_ionsecrc, encoding="utf-8")
        log.info(f"📝 wrote {node_dir / 'node.ionsecrc'}")


if __name__ == "__main__":
    main()
