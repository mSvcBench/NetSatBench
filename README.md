![Python](https://img.shields.io/badge/Python-3.8%2B-blue?style=for-the-badge&logo=python&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-20.10%2B-2496ED?style=for-the-badge&logo=docker&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)
![Status](https://img.shields.io/badge/Status-Active_Development-orange?style=for-the-badge)

<div align="center">

# NetSatBench
## Large-scale Satellite Network Benchmark System
### High-Scale • Distributed • Application-Agnostic

</div>


**NetSatBench** is a distributed emulation framework that supports the evaluation of communication and application workloads across large-scale satellite constellations.

> [!TIP]
> **The Core Innovation**
> : Unlike single-host emulators, NetSatBench distributes emulated satellites — implemented as Linux containers — scales out across a cluster of hosts to leverage *parallel computing resources*. It constructs a *dynamic Layer 2 network* interconnecting satellite antennas and ground terminals, enabling the enforcement of specific bandwidth and latency characteristics. The network connection are driven by a physics-based model that reflects *real-world satellite dynamics*. 

> [!NOTE]
> **Applications**
> : NetSatBench is  **L3 and application agnostic**; it provides a connected L2 satellite network fabric upon which any routing protocol (e.g., OSPF, BGP, IS-IS) or application (e.g., iperf, custom workloads) can be deployed and evaluated.

---

### Core Architecture

NetSatBench decouples simulation logic from physical proximity.
> [!IMPORTANT]
> **Three-Pillar Architecture**
>
> 1. **Distributed Execution and Control:** The satellite constellation is spread across a cluster of hosts (bare metal or VMs) and each satellite control itself -no central controller.
> 2. **L2 Fabric:** The framework dynamically establishes VXLAN L2 tunnels to encapsulate inter-satellite and staellite-ground traffic, ensuring the L2 connectivity remains continuous regardless of the underlying execution hosts.
> 3. **Scalability:** By distributing satellites/containers, the emulation can scale to thousands of satellites without bottlenecking the CPU or memory of a single machine.
> 4. **Physics-Driven Networking:** The network characteristics (latency, bandwidth) are derived from real-world LEO satellite physics, ensuring realistic emulation conditions.

---

### Repository Structure

The project is organized into two primary logical domains: the **Satellite Domain** (internal logic) and the **Infrastructure Domain** (external orchestration).

#### 1. Infrastructure Domain (scripts/)
The infrastructure domain run orchestration scripts that manage the initial deployment of the costellation across the distributed cluster and maintain the ongoing status withing a centralized Etcd key-value store. The main components include:
* **Constellation Configurator (`constellation-conf.py`):** Parses declarative JSON constellation topologies and pushes the global state to Etcd.
* **Constellation Builder (`constellation-builder.py`):** SSHes into distributed hostes to instantiate satellite containers.
* **Network Configurator (`network-configuration.py`):** Establishes VXLAN tunnels between containers and applies network characteristics using Linux Traffic Control (TC). -- TODO should be removed ---


#### 2. Satellite Domain (sat-container)
Defines the Linux container used to mimic a satellite node.

* **Internal Agents (`sat-agent-internal.py`):** A smart, persistent daemon acting as the satellite's autonomous controller.
    * **Continuous Sync:** Polls Etcd to stay in sync with the global simulation statue.
    * **Dynamic Topology:** Detects triggers to add/remove Inter-Satellite and Stellite-Ground Links as connectivity status changes, updating VXLAN links instantly without any L3 routing interruption.
    * **App Lifecycle:** Spins up or tears down on-board applications on demand.
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
