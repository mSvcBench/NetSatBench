# Scenarios
This directory contains sample satellite system configurations and epoch files for validation and benchmarking. Configurations are specified in JSON format as described in the [Configuration Manual](docs/configuration.md).
All files refers to a sample satellite system configuration file defining a constellation of 10 nodes, including 8 satellites of a single theoretical orbit sequentially connected, 1 ground station, and 1 user terminal. Ground station and user terminal are dynamically connected with two links to the satellite constellation.
Different IP versions and routing configurations are available for testing different scenarios:

- `sat-config.json`: uses automatic IPv4 addressing and IS-IS v4 routing
- `sat-config-ipv6.json`: uses automatic IPv6 addressing and IS-IS v6 routing
- `sat-config-or.json`: uses automatic IPv4 addressing and no IS-IS routing, intended for use with the oracle routing module --ip-version 4
- `sat-config-or-ipv6.json`: uses automatic IPv6 addressing and no IS-IS routing, intended for use with the oracle routing module --ip-version 6
- `epochs`: contains sample epoch files that define a sequence of link events (additions and removals) for the satellite system, without routing commands.