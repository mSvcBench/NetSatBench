![Python](https://img.shields.io/badge/Python-3.8%2B-blue?style=for-the-badge&logo=python&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-20.10%2B-2496ED?style=for-the-badge&logo=docker&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)
![Status](https://img.shields.io/badge/Status-Active_Development-orange?style=for-the-badge)

<div align="center">

# **NetSatBench**  
## *Large-Scale Satellite Network Benchmarking System*  
### High-Scale • Distributed • Application-Agnostic

</div>

**NetSatBench** is a distributed emulation framework for evaluating communication and application workloads across large-scale satellite constellations. It provides a scalable Layer 2 (L2) network substrate on which arbitrary routing protocols and applications can be deployed without modification.

---

## 🚀 Core Innovation

Unlike single-host emulators, **NetSatBench** distributes emulated satellites—implemented as Linux containers—across a cluster of machines, enabling high degrees of parallelism and scalability.  
VXLAN tunnels form a dynamic **L2 network fabric** interconnecting satellite antennas and ground terminals, while link characteristics (e.g., latency, bandwidth) follow a **physics-driven model of orbital dynamics**, closely reflecting real-world LEO behavior.

---

## 🛰️ Applications

NetSatBench is **L3- and application-agnostic**. Any routing protocol (e.g., OSPF, BGP, IS-IS, FRR) or user-defined application (e.g., iperf, traffic generators, analytics workloads) can run directly over the emulated constellation.

---

## 🏗️ Core Architecture

NetSatBench separates **simulation logic** from **physical execution**, enabling flexible deployment across clusters of heterogeneous hosts.

### **Four Architectural Pillars**

1. **Distributed Execution and Control**  
   Satellite nodes are instantiated across a cluster (bare metal or VMs). Each node manages its own lifecycle and networking logic—no central controller is required.

2. **Dynamic L2 Fabric**  
   VXLAN tunnels encapsulate inter-satellite and satellite–ground links, ensuring seamless L2 connectivity regardless of placement on physical hosts.

3. **Scalability Through Distribution**  
   By spreading containers across multiple machines, the system scales to thousands of satellites without saturating the resources of a single host.

4. **Physics-Driven Networking**  
   Link parameters are derived from orbital mechanics and line-of-sight geometry, ensuring realistic performance evaluation.

---

## 📁 Repository Structure

The project is organized into two major domains: the **Infrastructure Domain** and the **Satellite Domain**.

### 1. **Infrastructure Domain (`scripts/`)**

Responsible for orchestrating the deployment, configuration, and global state management of the constellation.

- **Constellation Configurator (`constellation-conf.py`)**  
  Parses declarative JSON topology descriptions and publishes constellation state to an Etcd cluster.

- **Constellation Builder (`constellation-builder.py`)**  
  Instantiates satellite containers on distributed hosts over SSH.

- **Network Configurator (`network-configuration.py`)**  
  Establishes VXLAN tunnels and configures link properties using Linux Traffic Control (TC).  
  *Note: marked for future consolidation or removal.*

### 2. **Satellite Domain (`sat-container/`)**

Defines the Linux container image and internal agents responsible for autonomous satellite behavior.

- **Internal Agent (`sat-agent-internal.py`)**  
  A persistent daemon that:
  - Synchronizes with constellation state stored in Etcd.  
  - Dynamically creates or removes inter-satellite and satellite–ground VXLAN links as topology changes.  
  - Manages the lifecycle of on-board applications.

---

## 🧩 Agnosticism & Workload Support

| Feature | Description |
|--------|-------------|
| **Routing-Agnostic** | Supports FRR/IS-IS out-of-the-box; compatible with OSPF, BGP, or custom routing modules. |
| **Net Benchmarking Tools** | Integrates naturally with `iperf`, `ping`, or custom traffic generators. |
| **Custom Applications** | Any Linux-compatible service or experiment can run inside each emulated satellite. |

---

## 🛠️ Prerequisites

Ensure the following are installed on all worker nodes:

- **Docker** — container runtime for satellite nodes  
- **Etcd** — distributed key-value store for global state coordination  
- **Python 3** — with libraries `etcd3` and `protobuf`  
- **Linux Bridge Utilities** — for VXLAN endpoint and switching configuration  

---

## ⚡ Quick Start

Deploying a constellation across your cluster:

```bash
cd scripts/

# 1. Synchronize topology state
python3 constellation-conf.py

# 2. Provision the constellation across remote hosts
python3 constellation-builder.py

# 3. Establish VXLAN connectivity and apply physics-based link parameters
python3 network-configuration.py
