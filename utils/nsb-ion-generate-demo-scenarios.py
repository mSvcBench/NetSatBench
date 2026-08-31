#!/usr/bin/env python3

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path


BASE_TIME = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
DEFAULT_RATE = "10mbit"
# Keep NetEm's one-way delay aligned with the whole-second OWLT rendered into
# the ION range plan for these deterministic demonstration scenarios.
DEFAULT_DELAY = "1s"
MULTI_BUNDLE_DELAY = "100ms"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCENARIO_ROOT = REPOSITORY_ROOT / "examples/10nodes-ion-dtn/epochs"

CHAIN_NODES = [
    "usr1",
    "sat1",
    "sat2",
    "sat3",
    "sat4",
    "sat5",
    "sat6",
    "sat7",
    "sat8",
    "grd1",
]
CHAIN_LINKS = list(zip(CHAIN_NODES, CHAIN_NODES[1:]))


def build_link_descriptor(
    endpoint1,
    endpoint2,
    *,
    rate=DEFAULT_RATE,
    ion_rate=None,
    delay=DEFAULT_DELAY,
    loss=0,
):
    """Build one link entry in the format consumed by NetSatBench epochs."""
    descriptor = {
        "endpoint1": endpoint1,
        "endpoint2": endpoint2,
        "rate": rate,
    }
    if ion_rate is not None:
        descriptor["ion-rate"] = ion_rate
    descriptor.update({"loss": loss, "delay": delay})
    return descriptor


def build_epoch(elapsed_seconds, *, links_add=(), links_update=(), links_del=()):
    """Build an epoch at a deterministic offset from the demo time base."""
    timestamp = BASE_TIME + timedelta(seconds=elapsed_seconds)
    return {
        "time": timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "links-del": list(links_del),
        "links-update": list(links_update),
        "links-add": list(links_add),
    }


def build_sequential_chain_epochs():
    """Open each next hop 6 s before closing the preceding hop."""
    epochs = [
        build_epoch(0, links_add=[build_link_descriptor(*CHAIN_LINKS[0])])
    ]
    for index in range(1, len(CHAIN_LINKS)):
        boundary = index * 45
        epochs.append(
            build_epoch(
                boundary - 6,
                links_add=[build_link_descriptor(*CHAIN_LINKS[index])],
            )
        )
        epochs.append(
            build_epoch(
                boundary,
                links_del=[build_link_descriptor(*CHAIN_LINKS[index - 1])],
            )
        )
    return epochs


def build_cloudy_ground_epochs(outage_seconds=90):
    """Build the first eight relays plus a delayed final ground contact."""
    epochs = [
        build_epoch(0, links_add=[build_link_descriptor(*CHAIN_LINKS[0])])
    ]
    for index in range(1, 8):
        boundary = index * 45
        epochs.append(
            build_epoch(
                boundary - 6,
                links_add=[build_link_descriptor(*CHAIN_LINKS[index])],
            )
        )
        epochs.append(
            build_epoch(
                boundary,
                links_del=[build_link_descriptor(*CHAIN_LINKS[index - 1])],
            )
        )

    final_boundary = 8 * 45
    ground_add = final_boundary - 6 + outage_seconds
    ground_event = build_epoch(
        ground_add, links_add=[build_link_descriptor(*CHAIN_LINKS[8])]
    )
    relay_close = build_epoch(
        final_boundary, links_del=[build_link_descriptor(*CHAIN_LINKS[7])]
    )
    epochs.extend(
        [ground_event, relay_close]
        if ground_add < final_boundary
        else [relay_close, ground_event]
    )
    return epochs


def build_multi_bundle_epochs():
    """Create two constrained service windows around one relay queue."""
    bottleneck = ("sat4", "sat5")
    def queue_link(
        endpoint1,
        endpoint2,
        *,
        rate="2.4mbit",
        ion_rate="2mbit",
    ):
        return build_link_descriptor(
            endpoint1,
            endpoint2,
            rate=rate,
            ion_rate=ion_rate,
            delay=MULTI_BUNDLE_DELAY,
        )

    bypassed_bottleneck = [
        queue_link(*link)
        for link in CHAIN_LINKS
        if link != bottleneck
    ]
    return [
        build_epoch(0, links_add=bypassed_bottleneck),
        build_epoch(
            90,
            links_add=[queue_link(*bottleneck, rate="10mbit")],
        ),
        build_epoch(
            135,
            links_del=[queue_link(*bottleneck, rate="10mbit")],
        ),
        build_epoch(
            188,
            links_add=[
                queue_link(*bottleneck, rate="10mbit", ion_rate="1mbit")
            ],
        ),
        build_epoch(
            233,
            links_del=[
                queue_link(*bottleneck, rate="10mbit", ion_rate="1mbit")
            ],
        ),
        build_epoch(
            278,
            links_add=[queue_link(*bottleneck, rate="10mbit")],
        ),
        # Keep the recovery contact open through the post-drain control cohort.
        build_epoch(585),
    ]


def build_handover_overlap_epochs():
    """Build the 61-epoch 6+1 handover treatment without an IP oracle.

    The native benchmark keeps the eight inter-satellite ring links open and
    moves the user and ground attachments once per minute. Each replacement
    attachment is created 6 seconds before the preceding attachment is removed
    (make-before-break). Empty epochs retain a 61-epoch trace and preserve the
    native link-add and traffic-injection clock.

    The two deletions at the beginning of the native cyclic trace refer to
    links from the preceding, unrecorded cycle.  They are intentionally absent
    here because a fresh NetSatBench deployment has no such active links.
    """
    ring_links = [
        build_link_descriptor(f"sat{index}", f"sat{index + 1}", delay="1ms")
        for index in range(1, 8)
    ] + [build_link_descriptor("sat8", "sat1", delay="1ms")]

    epochs = [build_epoch(-2)]
    epochs.extend(
        [
            build_epoch(
                0,
                links_add=(
                    ring_links
                    + [
                        build_link_descriptor("usr1", "sat1", delay="1ms"),
                        build_link_descriptor("usr1", "sat2", delay="1ms"),
                    ]
                ),
            ),
            build_epoch(2),
            build_epoch(
                5,
                links_add=[
                    build_link_descriptor("grd1", "sat7", delay="1ms"),
                    build_link_descriptor("grd1", "sat8", delay="1ms"),
                ],
            ),
            build_epoch(
                6,
                links_del=[
                    build_link_descriptor("usr1", "sat1", delay="1ms")
                ],
            ),
            build_epoch(
                11,
                links_del=[
                    build_link_descriptor("grd1", "sat7", delay="1ms")
                ],
            ),
            build_epoch(28),
            build_epoch(33),
            build_epoch(35),
        ]
    )

    for cycle in range(1, 8):
        user_new = (cycle + 1) % 8 + 1
        ground_new = (cycle - 1) % 8 + 1
        epochs.extend(
            [
                build_epoch(
                    cycle * 60,
                    links_add=[
                        build_link_descriptor(
                            "usr1", f"sat{user_new}", delay="1ms"
                        )
                    ],
                ),
                build_epoch(cycle * 60 + 2),
                build_epoch(
                    cycle * 60 + 5,
                    links_add=[
                        build_link_descriptor(
                            "grd1", f"sat{ground_new}", delay="1ms"
                        )
                    ],
                ),
                build_epoch(
                    cycle * 60 + 6,
                    links_del=[
                        build_link_descriptor(
                            "usr1", f"sat{cycle % 8 + 1}", delay="1ms"
                        )
                    ],
                ),
                build_epoch(
                    cycle * 60 + 11,
                    links_del=[
                        build_link_descriptor(
                            "grd1", f"sat{(cycle + 6) % 8 + 1}", delay="1ms"
                        )
                    ],
                ),
                build_epoch(cycle * 60 + 28),
                build_epoch(cycle * 60 + 33),
                build_epoch(cycle * 60 + 35),
            ]
        )
        if cycle == 7:
            del epochs[-5:]
            epochs.append(build_epoch(cycle * 60 + 7))

    return epochs


def build_demo_scenarios():
    """Return the complete reproducible 10-node ION demonstration suite."""
    all_chain_links = [
        build_link_descriptor(endpoint1, endpoint2)
        for endpoint1, endpoint2 in CHAIN_LINKS
    ]

    ring_access_links = [
        build_link_descriptor("usr1", "sat1"),
        build_link_descriptor("sat5", "grd1"),
    ]
    clockwise_ring_links = [
        build_link_descriptor("sat1", "sat2"),
        build_link_descriptor("sat2", "sat3"),
        build_link_descriptor("sat3", "sat4"),
        build_link_descriptor("sat4", "sat5"),
    ]
    counterclockwise_ring_links = [
        build_link_descriptor("sat1", "sat8", rate="5mbit"),
        build_link_descriptor("sat8", "sat7", rate="5mbit"),
        build_link_descriptor("sat7", "sat6", rate="5mbit"),
        build_link_descriptor("sat6", "sat5", rate="5mbit"),
    ]

    dynamic_capacity_epochs = [
        build_epoch(0, links_add=all_chain_links),
        build_epoch(
            10,
            links_update=[build_link_descriptor("sat4", "sat5", rate="1mbit")],
        ),
        build_epoch(
            20,
            links_update=[
                build_link_descriptor("sat4", "sat5", rate="100kbit")
            ],
        ),
        build_epoch(
            30,
            links_update=[build_link_descriptor("sat4", "sat5", rate="10mbit")],
        ),
        build_epoch(40, links_del=all_chain_links),
    ]

    return {
        "00-all-open": [build_epoch(0, links_add=all_chain_links)],
        "01-store-carry-forward": build_sequential_chain_epochs(),
        "02-cloudy-ground": build_cloudy_ground_epochs(),
        "03-alternate-path": [
            build_epoch(
                0,
                links_add=(
                    ring_access_links
                    + clockwise_ring_links
                    + counterclockwise_ring_links
                ),
            ),
            build_epoch(
                45,
                links_update=[
                    build_link_descriptor("sat2", "sat3", rate="3mbit")
                ],
            ),
            build_epoch(
                90,
                links_update=[
                    build_link_descriptor("sat2", "sat3", rate="1mbit")
                ],
            ),
            build_epoch(
                135,
                links_update=[
                    build_link_descriptor("sat2", "sat3", rate="100kbit")
                ],
            ),
            build_epoch(180, links_del=[build_link_descriptor("sat2", "sat3")]),
            build_epoch(
                225,
                links_add=[
                    build_link_descriptor("sat2", "sat3", rate="100kbit")
                ],
            ),
            build_epoch(
                270,
                links_update=[
                    build_link_descriptor("sat2", "sat3", rate="1mbit")
                ],
            ),
            build_epoch(
                315,
                links_update=[
                    build_link_descriptor("sat2", "sat3", rate="3mbit")
                ],
            ),
            build_epoch(
                360,
                links_update=[
                    build_link_descriptor("sat2", "sat3", rate="10mbit")
                ],
            ),
            build_epoch(
                405,
                links_update=[
                    build_link_descriptor("sat2", "sat3", rate="10mbit")
                ],
            ),
        ],
        "04-multi-bundle-queue": build_multi_bundle_epochs(),
        "05-short-contact-capacity": dynamic_capacity_epochs,
        "06-handover-overlap": build_handover_overlap_epochs(),
    }


def expected_epoch_files():
    """Return each generated epoch path and its canonical JSON content."""
    expected_files = {}
    for scenario_name, scenario_epochs in build_demo_scenarios().items():
        for epoch_index, epoch_data in enumerate(scenario_epochs):
            epoch_path = (
                SCENARIO_ROOT
                / scenario_name
                / f"NetSatBench-epoch{epoch_index}.json"
            )
            expected_files[epoch_path] = json.dumps(epoch_data, indent=2) + "\n"
    return expected_files


def check_scenarios():
    """Check that committed epoch files match the declarative scenarios."""
    expected_files = expected_epoch_files()
    actual_files = set(SCENARIO_ROOT.glob("*/NetSatBench-epoch*.json"))
    errors = []

    for epoch_path, expected_content in expected_files.items():
        if not epoch_path.is_file():
            errors.append(f"missing: {epoch_path.relative_to(REPOSITORY_ROOT)}")
        elif epoch_path.read_text(encoding="utf-8") != expected_content:
            errors.append(f"outdated: {epoch_path.relative_to(REPOSITORY_ROOT)}")

    for epoch_path in sorted(actual_files - set(expected_files)):
        errors.append(f"unexpected: {epoch_path.relative_to(REPOSITORY_ROOT)}")

    if errors:
        raise RuntimeError("scenario check failed:\n" + "\n".join(errors))
    print(f"checked {len(expected_files)} epoch files")


def write_scenarios():
    """Write the declarative scenario suite to its checked-in epoch files."""
    expected_files = expected_epoch_files()
    actual_files = set(SCENARIO_ROOT.glob("*/NetSatBench-epoch*.json"))

    for stale_path in actual_files - set(expected_files):
        stale_path.unlink()
    for epoch_path, content in expected_files.items():
        epoch_path.parent.mkdir(parents=True, exist_ok=True)
        epoch_path.write_text(content, encoding="utf-8")

    print(f"wrote {len(expected_files)} epoch files")


def main():
    parser = argparse.ArgumentParser(
        description="Generate the reproducible 10-node ION-DTN scenario suite."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify committed epoch files without changing them.",
    )
    args = parser.parse_args()

    if args.check:
        check_scenarios()
    else:
        write_scenarios()


if __name__ == "__main__":
    main()
