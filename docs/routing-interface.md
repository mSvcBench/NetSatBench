<div align="center">

<img src="images/netsatbench_logo.png" alt="NetSatBench Logo" width="200"/>

# Routing Module Interface Specification for NetSatBench

</div>


## Overview

NetSatBench supports pluggable routing modules that are invoked by the `sat-agent` in response to node lifecycle events and topology changes.

A routing module is a Python script that is usually located in the `sat-container/extra/routing/` directory and implements a well-defined interface.  
sat-agent is responsible for when its functions are called, while the routing module defines how routing is configured and updated after those calls.

Any node-controlled routing solution can be integrated into NetSatBench as long as it respects this interface.

---

## Execution Model

The routing module reacts to three types of events:

1. Node initialization  
2. Link addition  
3. Link removal  

For each event, sat-agent invokes a corresponding function in the routing module.

The routing module:
- Must not assume control over event timing
- Must not manage link creation or deletion
- Must only apply routing-related actions

---

## Mandatory Interface

A routing module compatible with NetSatBench must expose exactly the following functions.

---

### 1. `init(etcd_client, node_name) -> tuple[str, bool]`

#### Invocation Time
- Called once during node startup
- Triggered after node configuration is available in Etcd

#### Purpose
This function initializes the routing subsystem for the node.

It establishes the global routing state that remains valid across link changes.

For instance, in case of FRR routing, this function may set up initial configuration parameters (e.g., routers, loopbacks, etc.) through `vtysh` commands.

#### Expected Responsibilities
Typical actions include:
- Reading global and node-specific configuration from Etcd
- Assigning router identifiers or loopback addresses
- Generating routing configuration files
- Starting or restarting routing services
- Initializing protocol-wide parameters

#### Constraints
- Must be safe to call exactly once
- Must not assume any link is already active
- Must prepare the system for subsequent link updates

#### Return Value
- A human-readable status message
- A boolean flag indicating success (`True`) or failure (`False`)

---

### 2. `link_add(etcd_client, node_name, interface) -> tuple[str, bool]`

#### Invocation Time
- Called whenever a new network link becomes available
- The interface already exists at the system level

#### Purpose
This function enables routing on a newly added interface.

It connects the interface to the routing logic previously initialized by `init()`.

For instance, in case of FRR routing, this function may add new interface to a router through `vtysh` commands.

#### Expected Responsibilities
Typical actions include:
- Enabling routing on the specified interface
- Attaching the interface to a routing instance
- Applying interface-level routing parameters

#### Constraints
- Must affect only the specified interface
- Must be idempotent
- Must not restart or reinitialize the entire routing system

#### Return Value
- A descriptive status message
- A boolean flag indicating success or failure

---

### 3. `link_del(etcd_client, node_name, interface) -> tuple[str, bool]`

#### Invocation Time
- Called whenever a network link is removed

#### Purpose
This function disables routing on the specified interface and removes any associated routing state.

For instance, in case of FRR routing, this function may remove the interface from a router through `vtysh` commands.

#### Expected Responsibilities
Typical actions include:
- Detaching the interface from the routing subsystem
- Cleaning up protocol-specific state related to the link

#### Constraints
- Must not disrupt routing on other interfaces
- Must be safe to call even if the interface was already inactive
- Must not reset global routing configuration

#### Return Value
- A descriptive status message
- A boolean flag indicating success or failure


---

## Design Rationale

This interface ensures:
- Separation of concerns between NetSatBench L2 Network-fabric and routing logic
- Protocol independence, allowing different routing approaches
- Minimal coupling, enabling easy replacement or comparison of routing strategies

By adhering to this interface, developers can integrate new routing mechanisms into NetSatBench without modifying sat-agent or the core framework. However, the re-building of the sat-container image may be necessary to include the new routing module in the extra folder.

---

## Example Implementation
An example routing module implementing this interface using FRR can be found in the `sat-container/extra/routing/isis.py` file for IS-IS routing protocol IPv4 and in `sat-container/extra/routing/isisv6.py` for IPv6.
