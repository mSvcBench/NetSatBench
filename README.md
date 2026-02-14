![License](https://img.shields.io/badge/License-BSD2-green?style=for-the-badge)
![Status](https://img.shields.io/badge/Status-Active_Development-orange?style=for-the-badge)

<div align="center">

<img src="docs/images/netsatbench_logo.png" alt="NetSatBench Logo" width="200"/>

# **NetSatBench**

## Large-Scale Satellite Network Benchmarking

</div>

**NetSatBench** is a distributed emulation framework for evaluating communication protocols and application workloads over large-scale satellite systems.

The emulated system consists of satellites, ground stations, and user terminals, each implemented as a Linux container and distributed across a cluster of bare-metal or virtual machines. This architecture enables high parallelism and scalability.

The satellite network is realized as an overlay Layer-2 fabric, where connections (satellite-to-satellite, satellite-to-ground, etc.) are implemented as VXLAN tunnels. Their lifecycle and performance characteristics (e.g., latency, bandwidth, packet loss) follow those of the modeled satellite system in real time.

NetSatBench is Layer-3 and application agnostic: any routing protocol (e.g., OSPF, BGP, IS-IS), addressing scheme, or user-defined application can be evaluated without modification on the emulated satellite system. Automatic IP addressing, an ideal oracle-routing protocol, and native IS-IS routing are provided out of the box.

---

## 🧩 Emulation Architecture

<div align="center">
<img src="docs/images/netsatbench-arch.png" alt="NetSatBench System Architecture" width="600"/>
</div>

### Distributed Execution

Emulated nodes are instantiated across a cluster of hosts—either bare-metal machines or virtual machines—referred to as *workers*. Nodes are placed according to a scheduling policy that accounts for both worker resource availability and the resource requests and limits defined for each emulated node.

---

### Distributed Control

The global state of the satellite system is continuously maintained in a distributed **Etcd** key–value store, which serves as the coordination backbone of NetSatBench. State information describing nodes, links, and scheduled tasks is published to **Etcd**. Each emulated node runs a local `sat-agent` that subscribes to the relevant portions of the store and translates state changes into local actions, such as link reconfiguration or task execution.

---

### Dynamic Layer-2 Fabric

Node-to-node links, including inter-satellite links (ISLs) and satellite-to-ground links (SGLs), are modeled as VXLAN tunnels that are dynamically created and managed by each node’s `sat-agent` according to the system state stored in **Etcd**.

This overlay fabric provides transparent Layer-2 connectivity among emulated nodes, independently of their physical placement within the cluster.

---

### Custom IP Routing

Upon link creation or removal, each `sat-agent` may invoke a user-provided IP routing module through a Python interface. This module is responsible for updating the routing daemon or performing custom routing actions over the VXLAN fabric.

Built-in IS-IS routing modules for FRR are provided for both IPv4 and IPv6, serving as reference implementations and examples of how custom routing logic can be integrated with the `sat-agent`.

---

### Automatic IPv4/IPv6 Addressing and Built-in Name Resolution

NetSatBench can optionally assign overlay IPv4 and IPv6 addresses automatically to all emulated nodes, which are routed over the VXLAN fabric. When enabled, the `/etc/hosts` file of each container is automatically populated with the names and overlay IP addresses of all nodes, enabling name resolution without requiring a dedicated DNS server.

---

### On-board Tasks and Application Execution

User-defined applications and tasks can be executed on any node by injecting their commands or scripts into the Etcd key–value store. The container image used for each emulated node (see `sat-container/Dockerfile`) is based on `python:3.11-slim` (Debian) and includes common networking utilities such as `ping`, `tcpdump`, and `iperf3`.

Additional software can be installed dynamically by scheduling installation tasks (e.g., `apt-get`), followed by tasks that execute the newly installed applications.

---

### Trace-Driven and Real-Time Emulation

System state evolution in **Etcd** is controlled through *epoch* JSON files. Each epoch defines link creation, updates, removals, and the scheduling of tasks or applications. This design supports:

* **Trace-driven emulation**, where network dynamics are predefined through recorded or generated epoch files.
* **Real-time (digital-twin) emulation**, where external processes dynamically generate new epoch files and inject them into the **Etcd** store to reflect real-time changes in the satellite system.

---

### Built-in Constellation Trace Generators

Epoch files can be generated using existing physical-layer satellite simulators integrated with NetSatBench-specific plugins that convert simulator outputs into the required event-driven epoch format.

Currently supported plugins include:

* **[StarPerf_Simulator](https://github.com/SpaceNetLab/StarPerf_Simulator)**: This plugin extends the simulator’s output by incorporating ground station and user link dynamics, as well as link bit rate and packet loss characteristics. It also provides a script to convert the extended simulation output into NetSatBench epoch files.

---

## 📁 Repository Structure

**control/**
Python scripts implementing orchestration functions, including cluster configuration and run-time control of the satellite system evolution.

**sat-container/**
Files used to build the container image for each emulated node.

**generators/**
Scripts for generating satellite system configurations and epoch files.

**examples/**
Sample emulated satellite systems for validation and benchmarking. 

**utils/**
Utility scripts for analysis, routing, and data processing.

**docs/**
Documentation files, including:

* [Control Commands](docs/commands.md)
* [Configuration Files](docs/configuration.md)
* [Etcd Key-Value Store](docs/etcd.md)
* [StarPerf Simulator Integration](docs/starperf-integration.md)
* [Routing Interface](docs/routing-interface.md)
* [Utils](docs/utils.md)

---

## 🛠️ Cluster Architecture

The emulation cluster consists of two logical host roles:

* **control host**
* **workers**

A single physical or virtual host may act as both control host and worker.

In typical deployments, control and worker hosts are Linux virtual machines or bare-metal servers connected through a 10 Gbps (or higher) Ethernet network. In our experiments, we used OpenStack virtual machines running Ubuntu 24.04.

> **No IP Spoofing**
> VXLAN tunnels use the IP addresses of the containers’ `eth0` interfaces as tunnel endpoints. Therefore, the underlying network must allow direct IP connectivity between container subnets (`sat-vnet`) across different worker hosts, without IP spoofing protection mechanisms.
>
> In cloud environments, security policies applied to host interfaces must allow unrestricted traffic among all container subnets (`sat-vnet-super-cidr`).

---

## 📱 Software Requirements

### Control Host

The control host must have SSH access to all workers using key-based authentication. It executes orchestration scripts and runs an **Etcd** instance, which maintains the global state of the emulated satellite system.

Required software:

* **Etcd** — distributed key–value store for global coordination
* **Python3** — with dependencies specified in `requirements.txt`
* **SSH client** — for remote access to workers

---

### Worker Hosts

Workers are Linux hosts on which emulated nodes (Linux containers) are instantiated.

Each worker must allow passwordless `sudo` access for the SSH user used by the control host. This is required to execute `iptables` commands that enable direct inter-container communication without NAT.

Required software:

* **Docker** — for running containerized nodes. The SSH user must belong to the `docker` group
* **SSH server** — to allow remote access from the control host without password and with sudo privileges

---

## ⚡ Quick Start

Clone or download the repository on the control host and follow these steps to deploy and run a sample emulated satellite system. Ensure that all software requirements on both control and worker hosts are satisfied.

The sample configuration files are located in [`examples/10nodes`](examples/10nodes).
The cluster consists of two workers, `host-1` and `host-2`, defined in [`workers-config.json`](examples/10nodes/workers-config.json). For simplicity, `host-1` also acts as the control host.

The emulated system includes 8 satellites, 1 ground station, and 1 user, as defined in [`sat-config.json`](examples/10nodes/sat-config.json). IP addressing is automatically managed and IS-IS routing is used for Layer-3 connectivity. Use [`sat-config-v6.json`](examples/10nodes/sat-config-v6.json) for IPv6.

The dynamic evolution of the satellite system (link creation, updates, removal, and task execution) is defined through epoch files located in [`examples/10nodes/epochs`](examples/10nodes/epochs). The ground station `gdr1` runs an `iperf3` server starting from the initial epoch.

### 1. Customize Configuration

* **Mandatory** — Edit `workers-config.json` to specify worker IP addresses and SSH parameters.
* **Optional** — Edit `sat-config.json` to customize static parameters (e.g., node names, container images).
* **Optional** — Edit epoch files in `epochs/` to modify dynamic behavior such as link creation, updates, removal, and task scheduling.

### 2. Cluster Initialization

From the control host, configure the environment variables required to access the Etcd store:

```bash
export ETCD_HOST="10.0.1.215"
export ETCD_PORT="2379"
```

If Etcd authentication or TLS is enabled, set the following optional parameters:

```bash
export ETCD_USER="username"
export ETCD_PASSWORD="password"
export ETCD_CA_CERT="/path/to/ca.crt"
```

Initialize the worker environment:

```bash
python3 control/system-init-docker.py --config ./examples/10nodes/workers-config.json
```

### 3. Initialize, Deploy, and Run the Emulated Satellite System

Push static satellite system information to Etcd:

```bash
python3 control/nsb-init.py --config ./examples/10nodes/sat-config.json
```

Deploy the emulated nodes of the satellite system on workers:

```bash
python3 control/nsb-deploy.py
```

Start the emulation by executing dynamic events from the epoch files:

```bash
python3 control/nsb-run.py --loop-delay 60
```

### 4. Monitoring and Interaction

You can monitor and interact with emulated nodes by connecting to the containers running on worker hosts via SSH. The `utils/nsb-exec.py` script simplifies this process.

Examples:

Run a bash shell on container `usr1`:

```bash
python3 utils/nsb-exec.py -it usr1 bash
```

Display the routing table on `usr1`:

```bash
python3 utils/nsb-exec.py usr1 ip route show
```

Run an `iperf3` client from `usr1` to ground station `grd1`:

```bash
python3 utils/nsb-exec.py usr1 iperf3 -c grd1 -t 30 -i 2
```

### 5. Cleanup

After completing your experiments, remove the emulated satellite system from workers:

```bash
python3 control/nsb-rm.py
```

Optionally, remove residual configuration from worker hosts (required only if changing worker settings):

```bash
python3 control/system-cleanup-docker.py
```
