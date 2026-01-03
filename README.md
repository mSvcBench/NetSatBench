![Python](https://img.shields.io/badge/Python-3.8%2B-blue?style=for-the-badge&logo=python&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-20.10%2B-2496ED?style=for-the-badge&logo=docker&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)
![Status](https://img.shields.io/badge/Status-Active_Development-orange?style=for-the-badge)

<div align="center">

<img src="docs/images/netsatbench_logo.png" alt="NetSatBench Logo" width="300"/>

# **NetSatBench**  
## *Large-Scale Satellite Network Benchmarking System*  

</div>

**NetSatBench** is a distributed emulation framework for evaluating communication and application workloads across large-scale satellite constellations. It provides a scalable Layer 2 (L2) network substrate on which arbitrary routing protocols and applications can be deployed without modification.

Emulated satellite systems are made of satellites, ground stations and user terminals, whose nodes are implemented as **Linux containers**, across a cluster of machines, enabling high degrees of parallelism and scalability.  
VXLAN tunnels form a dynamic **L2 network fabric** interconnecting emulated nodes with specific link characteristics (e.g., latency, bandwidth, delay) to mimic real-world satellite network behavior.

NetSatBench is **L3- and application-agnostic**. Any routing protocol (e.g., OSPF, BGP, IS-IS) or user-defined application (e.g., iperf, traffic generators, analytics workloads) can run directly over the emulated constellation. IS-IS routing is supported out-of-the-box via FRR.

---

## <img src="docs/images/arch_core.png" alt="NetSatBench Logo" width="26"/> Emulation Architecture

**Distributed Execution and Control**  
   Emulated nodes are instantiated across a cluster (bare metal or VMs). Each emulated node manages its own lifecycle and configuration via an internal control-plane agent, coordinated through a distributed key-value store (Etcd).

**Dynamic L2 Fabric**  
   VXLAN tunnels encapsulate data-plane node-to-node traffic, ensuring seamless L2 connectivity regardless of container placement on physical hosts.

**Scalability Through Distribution**  
   By spreading containers across multiple machines, the emulation can scale to thousands of satellites without overwhelming a single host.

**Physics-Driven Networking**  
   Link parameters are derived from orbital mechanics and line-of-sight geometry, ensuring realistic performance evaluation.

---

## 📁 Repository Structure

**Orchestration** - The folder contains Python scripts that manage constellation-wide orchestration tasks.
Responsible for configuring the cluster of the host and orchestrating the deployment and run-time evolution of the constellation.

**Satellite Agent** - Contains the software for building the container image of an emulation node.

**Test** - Contains sample constellation topologies for validation and benchmarking.

**Docs** - Contains documentation assets, including images and diagrams.

## 🛠️ Cluster Architecture 
The cluster used for emulation is made of two types of hosts: 

- **control host**
- **workers**

An host of the cluster can act as both control host and worker. 

Typically, control host and workers are virtual machines or bare-metal servers conected by an 10+ gigabit ethernet.

## 📱 Software Requirements

### Control host
The control host should have ssh access to all worker hosts with key-based authentication.

It will execute orchestration scripts and run an instance of the **etcd** key-value store used to store the global state of the emulation.

The following software must be installed: 
- **Etcd** — distributed key-value store for global state coordination. `Etcd Authentication` should be configred so that the user and password used by the orchestration scripts and the satellite agent have read and write permissions to the key-value store.  
- **Python 3** — with libraries specified in `requirements.txt`  

Ensure the following are installed on the `worker hosts` machines where emulated nodes will be instantiated:

### Worker
A worker should have no-password sudo access fot the ssh user used by the control host to connect to it.

The following software must be installed:
- **Docker** — for containerized emulated nodes. The user used by the control host to connect to the worker must have permissions to run docker commands, i.e., sould be added to the docker group. 
  
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
