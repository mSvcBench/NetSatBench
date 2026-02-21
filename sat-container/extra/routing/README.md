# NetSatNEnch Routing Modules

## IS-IS Routing
- File: `utils/isis.py`
- Description: This module is called by the sat-agent to configure IS-IS routing with IPv4 support on each interface of the node. Requires a IPv4 CIDR block assigned to the node in the sat-config.json file. 

## IS-IS Routing with IPv6
- File: `utils/isisv6.py`
- Description: This module is called by the sat-agent to configure IS-IS routing with IPv6 support on each interface of the node. Requires a IPv6 CIDR block assigned to the node in the sat-config.json file. 

## Connected-Only IPv6 Routing
- File: `extra/routing/single_hop_v6.py`
- Description: This module is called by the sat-agent and configure IPv6 single-hop routing on each interface of the node. Requires a IPv6 CIDR block assigned to the node in the sat-config.json file.

