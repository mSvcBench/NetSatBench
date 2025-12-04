![Python](https://img.shields.io/badge/Python-3.8%2B-blue?style=for-the-badge&logo=python&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-20.10%2B-2496ED?style=for-the-badge&logo=docker&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)
![Status](https://img.shields.io/badge/Status-Active_Development-orange?style=for-the-badge)

<div align="center">

# NetSatBench
## Distributed LEO Satellite Emulator
### High-Scale • Distributed • Application-Agnostic

</div>

## Abstract

**NetSatBench** is a high-scale emulator designed to model Low Earth Orbit (LEO) satellite constellations across a distributed infrastructure.

> [!TIP]
> **The Core Innovation**
> Unlike single-host simulators, NetSatBench spans multiple hosts to maximize compute resources. We use **VXLAN tunneling** to create a seamless overlay network, allowing satellite containers on different physical hosts to communicate as if they were adjacent nodes in orbit.

> [!NOTE]
> **Design Philosophy**
> NetSatBench provides a transparent L2 fabric. While we support standard routing (FRR/IS-IS), the platform is **application-agnostic**; we provide the wires, you choose what runs over them.

---

### Core Architecture: The VXLAN Fabric

NetSatBench decouples simulation logic from physical proximity.
> [!IMPORTANT]
> **Three-Pillar Architecture**
>
> 1.  **Distributed Execution:** The constellation is partitioned across a cluster of physical workers (bare metal or VMs).
> 2.  **VXLAN Overlays:** The framework dynamically establishes VXLAN tunnels to encapsulate inter-satellite traffic, ensuring the "virtual space" remains continuous regardless of the underlying physical topology.
> 3.  **Scalability:** By distributing containers, the emulation can scale to thousands of nodes without bottlenecking the CPU or memory of a single machine.

---

### Repository Structure

The project is organized into two primary logical domains: the **Infrastructure Plane** (internal logic) and the **Control Plane** (external orchestration).

#### 1. sat-container/ (Infrastructure Plane)
Defines the satellite node environment and the "shell" for applications.

* **Docker Context:** Specifications for the base satellite image (Ubuntu-based), including network stacks and SSH services.
* **Internal Agents (`sat-agent-internal.py`):** A smart, persistent daemon acting as the node's autonomous controller.
    * **Continuous Sync:** Polls Etcd to stay in sync with the global simulation state.
    * **Dynamic Topology:** Detects triggers to add/remove Inter-Satellite Links (ISLs) as line-of-sight changes, updating VXLAN/Bridge interfaces instantly without restarts.
    * **App Lifecycle:** Spins up applications or routing processes on demand.

#### 2. scripts/ (Control Plane)
The centralized controller that manages the distributed cluster.

* **Topology Management:** Python scripts that parse declarative JSON topologies and sync the global state to Etcd.
* **Provisioning:** Automation tools that SSH into distributed workers to instantiate containers.
* **Network Controller:** Applies link characteristics (latency, bandwidth, jitter) via Linux Traffic Control (TC), strictly adhering to LEO physics constraints.

---

### Agnosticism & Workloads

NetSatBench is designed to be flexible.

| Feature | Description |
| :--- | :--- |
| **Optional Routing** | Includes reference implementations for **FRR** and **IS-IS**, but supports OSPF, BGP, or custom SDN controllers. |
| **Benchmarking** | Ready for `iperf`, `ping`, or custom traffic generators. |
| **Custom Apps** | Any Linux-compatible application can be deployed within the satellite containers. |

---

### Prerequisites

Before running the simulation, ensure the following are installed:

* **Docker:** Required on all physical worker nodes.
* **Etcd:** A running cluster for distributed state management.
* **Python 3:** With `etcd3` and `protobuf` libraries installed.
* **Linux Bridge:** For handling local switching and VXLAN endpoints.

---

### Quick Start

To deploy a simulation scenario across your distributed cluster, navigate to the orchestration directory and execute the lifecycle scripts in order:

```bash
cd scripts/

# 1. Synchronize topology state
# Parses the constellation geometry and pushes state to Etcd
python3 constellation-conf.py

# 2. Provision infrastructure
# Connects to remote hosts via SSH to spin up Docker containers
python3 constellation-builder.py

# 3. Establish Connectivity
# Sets up VXLAN tunnels and applies network physics
python3 network-configuration.py
