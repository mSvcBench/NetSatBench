#!/usr/bin/env python3

import argparse
from datetime import datetime, timedelta, timezone
import json
import logging
import re
from pathlib import Path

logging.basicConfig(level="INFO", format="[%(levelname)s] %(message)s")
log = logging.getLogger("nsb-ion-generate-configs")

DEFAULT_CONTACT_DURATION_SECONDS = 600


def load_spec(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


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


def contacts_from_epochs(spec, epoch_dir, file_pattern, epoch_duration):
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
    nodes = spec["nodes"]

    for epoch_path, epoch, timestamp in loaded_epochs:
        start = epoch_offset_seconds(timestamp, first_timestamp, epoch_path)

        for action in ["links-add", "links-del", "links-update"]:
            for link in epoch.get(action, []):
                left, right = link_nodes(link)
                if left not in nodes or right not in nodes:
                    raise ValueError(
                        f"epoch link references node not in ION spec: "
                        f"{epoch_path}: {left} <-> {right}"
                    )

        for link in epoch.get("links-del", []):
            left, right = link_nodes(link)
            key = link_key(left, right)
            active = active_links.pop(key, None)
            if active is None:
                continue

            active_left, active_right, active_start, source_epoch = active
            if start > active_start:
                contacts.append(
                    {
                        "from": active_left,
                        "to": active_right,
                        "start": active_start,
                        "end": start,
                        "source_epoch": f"{source_epoch}..{epoch_path.name}",
                    }
                )

        for link in epoch.get("links-add", []):
            left, right = link_nodes(link)
            key = link_key(left, right)
            if key in active_links:
                continue

            active_links[key] = (left, right, start, epoch_path.name)

    scenario_end = (
        epoch_offset_seconds(loaded_epochs[-1][2], first_timestamp, loaded_epochs[-1][0])
        + epoch_duration
    )
    for active_left, active_right, active_start, source_epoch in active_links.values():
        if scenario_end > active_start:
            contacts.append(
                {
                    "from": active_left,
                    "to": active_right,
                    "start": active_start,
                    "end": scenario_end,
                    "source_epoch": f"{source_epoch}..end",
                }
            )

    if not contacts:
        raise ValueError(f"no active contacts found under {epoch_dir}")

    return contacts


def ion_node(spec, name):
    return int(spec["nodes"][name]["ion_node"])


def node_id(spec, node_name):
    return int(spec["nodes"][node_name]["ion_node"])


def overlay_ip(spec, node_name):
    return spec["nodes"][node_name]["overlay_ip"]


def contact_nodes(contact):
    if isinstance(contact, dict):
        return contact["from"], contact["to"]

    return contact[0], contact[1]


def contact_window(spec, contact):
    if isinstance(contact, dict):
        return int(contact["start"]), int(contact["end"])

    return 0, int(spec["contact_duration_seconds"])


def render_contact_time(spec, offset_seconds):
    start_time = spec.get("_absolute_start_time")
    if start_time is None:
        return f"+{int(offset_seconds)}"

    return format_ion_time(start_time + timedelta(seconds=int(offset_seconds)))


def contact_source(contact):
    if isinstance(contact, dict):
        return contact.get("source_epoch")

    return None


def validate_spec(spec):
    required = [
        "experiment",
        "udp_port",
        "contact_rate_bytes_per_second",
        "range_seconds",
        "nodes",
    ]

    for key in required:
        if key not in spec:
            raise ValueError(f"missing required key: {key}")

    path = spec.get("path", [])
    nodes = spec["nodes"]
    contacts = spec.get("contacts")

    for name in path:
        if name not in nodes:
            raise ValueError(f"path node is not defined in nodes: {name}")

    for key in ["default_source", "default_destination"]:
        node_name = spec.get(key)
        if node_name and node_name not in nodes:
            raise ValueError(f"{key} node is not defined in nodes: {node_name}")

    if contacts is None:
        if len(path) < 2:
            raise ValueError(
                "contacts are required unless --epoch-dir derives them or path "
                "contains at least two nodes"
            )
        contacts = [[left, right] for left, right in zip(path, path[1:])]
        spec["contacts"] = contacts

    for contact in contacts:
        if isinstance(contact, dict):
            for key in ["from", "to", "start", "end"]:
                if key not in contact:
                    raise ValueError(f"contact is missing required key {key}: {contact}")
            if int(contact["start"]) < 0:
                raise ValueError(f"contact start must be non-negative: {contact}")
            if int(contact["end"]) <= int(contact["start"]):
                raise ValueError(f"contact end must be greater than start: {contact}")
        elif len(contact) != 2:
            raise ValueError(f"contact must contain exactly two nodes: {contact}")

        for name in contact_nodes(contact):
            if name not in nodes:
                raise ValueError(f"contact node is not defined in nodes: {name}")

    ion_ids = [int(node["ion_node"]) for node in nodes.values()]
    if len(ion_ids) != len(set(ion_ids)):
        raise ValueError("ion_node values must be unique")

    overlay_ips = [node["overlay_ip"] for node in nodes.values()]
    if len(overlay_ips) != len(set(overlay_ips)):
        raise ValueError("overlay_ip values must be unique")

    for value_key in [
        "udp_port",
        "contact_rate_bytes_per_second",
        "range_seconds",
    ]:
        if float(spec[value_key]) <= 0:
            raise ValueError(f"{value_key} must be positive")

    if int(spec.get("contact_duration_seconds", DEFAULT_CONTACT_DURATION_SECONDS)) <= 0:
        raise ValueError("contact_duration_seconds must be positive when set")


def direct_neighbors(spec, node_name):
    neighbors = []
    for contact in spec["contacts"]:
        left, right = contact_nodes(contact)
        if left == node_name:
            neighbors.append(right)
        elif right == node_name:
            neighbors.append(left)

    return sorted(set(neighbors))


def render_global_ionrc(spec):
    source = spec.get("default_source", "?")
    destination = spec.get("default_destination", "?")
    path = spec.get("path")
    contacts = spec["contacts"]
    rate = int(spec["contact_rate_bytes_per_second"])
    range_seconds = spec["range_seconds"]

    lines = [
        "# Generated ION contact/range plan for NetSatBench ION-DTN prototype.",
        "# Source: path-spec.json",
        "#",
        f"# Experiment: {spec['experiment']}",
        (
            f"# Primary path: {' -> '.join(path)}"
            if path
            else f"# Default flow: {source} -> {destination}"
        ),
        "#",
        (
            f"# Absolute time base: {format_ion_time(spec['_absolute_start_time'])} UTC."
            if spec.get("_absolute_start_time")
            else "# Relative times below are anchored at ionadmin execution time."
        ),
        "# Regenerate before each clean run if path-spec.json changes.",
        "",
        "# Ranges declare one-way light time between adjacent ION nodes.",
    ]

    for contact in contacts:
        left, right = contact_nodes(contact)
        start, end = contact_window(spec, contact)
        start_text = render_contact_time(spec, start)
        end_text = render_contact_time(spec, end)
        a = ion_node(spec, left)
        b = ion_node(spec, right)
        source = contact_source(contact)
        if source:
            lines.append(f"# {source}: {left} <-> {right}")
        lines.append(f"a range {start_text} {end_text} {a} {b} {range_seconds}")
        lines.append(f"a range {start_text} {end_text} {b} {a} {range_seconds}")

    lines.extend([
        "",
        "# Contacts declare planned transmission opportunities and byte-per-second rate.",
    ])

    for contact in contacts:
        left, right = contact_nodes(contact)
        start, end = contact_window(spec, contact)
        start_text = render_contact_time(spec, start)
        end_text = render_contact_time(spec, end)
        a = ion_node(spec, left)
        b = ion_node(spec, right)
        source = contact_source(contact)
        if source:
            lines.append(f"# {source}: {left} <-> {right}")
        lines.append(f"a contact {start_text} {end_text} {a} {b} {rate}")
        lines.append(f"a contact {start_text} {end_text} {b} {a} {rate}")

    return "\n".join(lines) + "\n"


def render_node_bprc(spec, node_name):
    ion_id = node_id(spec, node_name)
    udp_port = int(spec["udp_port"])
    neighbors = direct_neighbors(spec, node_name)

    lines = [
        f"# Generated BP configuration for NetSatBench node {node_name}.",
        f"# ION node: {ion_id}",
        "# Source: path-spec.json",
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
            f"a outduct udp {overlay_ip(spec, neighbor_name)}:{udp_port} udpclo"
        )

    lines.extend([
        "r 'ipnadmin node.ipnrc'",
        "s",
    ])

    return "\n".join(lines) + "\n"


def render_node_ipnrc(spec, node_name):
    udp_port = int(spec["udp_port"])
    rate = int(spec["contact_rate_bytes_per_second"])

    lines = [
        f"# Generated IPN/CGR neighbor plan for NetSatBench node {node_name}.",
        f"# ION node: {node_id(spec, node_name)}",
        "# Source: path-spec.json",
        "# CGR uses the contact graph plus these direct-neighbor plans.",
        "",
    ]

    neighbors = direct_neighbors(spec, node_name)
    for neighbor_name in neighbors:
        lines.append(f"# Neighbor: {neighbor_name}")
        lines.append(
            f"a plan {node_id(spec, neighbor_name)} "
            f"udp/{overlay_ip(spec, neighbor_name)}:{udp_port} {rate}"
        )

    return "\n".join(lines).rstrip() + "\n"


def render_node_ionconfig(spec, node_name):
    ion_id = node_id(spec, node_name)
    wm_key = 90000 + ion_id

    lines = [
        f"# Generated ION local node configuration for NetSatBench node {node_name}.",
        f"# ION node: {ion_id}",
        "# Source: path-spec.json",
        "",
        f"wmKey {wm_key}",
        f"sdrName ion{ion_id}",
        "wmSize 5000000",
        "configFlags 1",
        "heapWords 2000000",
        "pathName /tmp",
    ]

    return "\n".join(lines) + "\n"


def render_node_ionrc(spec, node_name):
    ion_id = node_id(spec, node_name)
    horizon = int(spec["contact_duration_seconds"])

    lines = [
        f"# Generated ION startup script for NetSatBench node {node_name}.",
        f"# ION node: {ion_id}",
        "# Source: path-spec.json",
        "",
        f"1 {ion_id} node.ionconfig",
        "s",
        f"m horizon +{horizon}",
        "r 'ionadmin ../global.ionrc'",
    ]

    return "\n".join(lines) + "\n"


def render_node_ionsecrc(spec, node_name):
    return ""


def main():
    parser = argparse.ArgumentParser(
        description="Generate ION config files from a NetSatBench DTN node spec."
    )
    parser.add_argument("--spec", required=True, help="Path to path-spec.json")
    parser.add_argument("--output", required=True, help="Output directory")
    parser.add_argument(
        "--epoch-dir",
        help=(
            "Optional NetSatBench epoch directory. If set, links-add and "
            "links-del entries are replayed to derive active contact windows."
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
    args = parser.parse_args()

    spec_path = Path(args.spec)
    output_dir = Path(args.output)

    spec = load_spec(spec_path)
    spec.setdefault("contact_duration_seconds", DEFAULT_CONTACT_DURATION_SECONDS)
    if args.absolute_start_time:
        spec["_absolute_start_time"] = parse_utc_datetime(args.absolute_start_time)

    if args.epoch_dir:
        if args.epoch_duration is None:
            raise ValueError("--epoch-duration is required when --epoch-dir is set")
        if args.epoch_duration <= 0:
            raise ValueError("--epoch-duration must be positive")

        spec["contacts"] = contacts_from_epochs(
            spec,
            args.epoch_dir,
            args.file_pattern,
            args.epoch_duration,
        )
        spec["contact_duration_seconds"] = max(
            int(spec.get("contact_duration_seconds", DEFAULT_CONTACT_DURATION_SECONDS)),
            max(contact["end"] for contact in spec["contacts"]) + args.epoch_duration,
        )

    validate_spec(spec)

    output_dir.mkdir(parents=True, exist_ok=True)

    global_ionrc = render_global_ionrc(spec)
    (output_dir / "global.ionrc").write_text(global_ionrc, encoding="utf-8")
    log.info(f"📝 wrote {output_dir / 'global.ionrc'}")

    for node_name in sorted(spec["nodes"]):
        node_dir = output_dir / node_name
        node_dir.mkdir(parents=True, exist_ok=True)

        node_bprc = render_node_bprc(spec, node_name)
        (node_dir / "node.bprc").write_text(node_bprc, encoding="utf-8")
        log.info(f"📝 wrote {node_dir / 'node.bprc'}")

        node_ipnrc = render_node_ipnrc(spec, node_name)
        (node_dir / "node.ipnrc").write_text(node_ipnrc, encoding="utf-8")
        log.info(f"📝 wrote {node_dir / 'node.ipnrc'}")

        node_ionconfig = render_node_ionconfig(spec, node_name)
        (node_dir / "node.ionconfig").write_text(node_ionconfig, encoding="utf-8")
        log.info(f"📝 wrote {node_dir / 'node.ionconfig'}")

        node_ionrc = render_node_ionrc(spec, node_name)
        (node_dir / "node.ionrc").write_text(node_ionrc, encoding="utf-8")
        log.info(f"📝 wrote {node_dir / 'node.ionrc'}")

        node_ionsecrc = render_node_ionsecrc(spec, node_name)
        (node_dir / "node.ionsecrc").write_text(node_ionsecrc, encoding="utf-8")
        log.info(f"📝 wrote {node_dir / 'node.ionsecrc'}")


if __name__ == "__main__":
    main()
