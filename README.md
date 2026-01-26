![License](https://img.shields.io/badge/License-BSD2-green?style=for-the-badge)
![Status](https://img.shields.io/badge/Status-Active_Development-orange?style=for-the-badge)

<div align="center">

<img src="docs/images/netsatbench_logo.png" alt="NetSatBench Logo" width="200"/>

# **NetSatBench**  
## Large-Scale Satellite Network Benchmarking

</div>

**NetSatBench** is a distributed emulation framework for evaluating communication and application workloads over large-scale satellite constellations.

Emulated systems consist of satellites, ground stations, and user terminals, each implemented as a **Linux container** and distributed across a cluster of bare-metal or virtual machines. This design enables a high degree of parallelism and scalability.  

The satellite network is implemented as an **overlay Layer-2 fabric** whose connections (satellite-to-satellite, satellite-to-ground, etc.) are **VXLAN** tunnels.  Their lifecycle and performance characteristics (e.g., latency, bandwidth, loss) follow those of the connections in the constellation under consideration in real time.  

NetSatBench is **layer 3 and application agnostic**: any routing protocol (e.g., OSPF, BGP, IS-IS), addressing scheme, or user-defined application can be evaluated without modification on the emulated constellation. However, automatic IP addressing, an ideal *oracle-routing* protocol, and real IS-IS routing are supported out of the box, if desired.

---

## 🧩 Emulation Architecture

<div align="center">
<img src="docs/images/netsatbench-arch.png" alt="NetSatBench System Architecture" width="600"/>
</div>

### Distributed Execution

Emulated nodes are instantiated across a cluster of hosts—either bare metal
machines or virtual machines—referred to as *workers*. Nodes are placed according
to a scheduling logic that accounts for both worker resource availability and the
resource requirements (requests and limits) specified for each emulated node.

---

### Distributed Control

The global state of the emulated constellation is continuously maintained in a
distributed **Etcd** key–value store, which acts as the coordination backbone of
NetSatBench. Information about nodes, links, and scheduled tasks is published to
Etcd, while each emulated node runs an internal `sat-agent` that subscribes to the
relevant portions of the store.

Based on these updates, each `sat-agent` enforces the desired local state, such as
creating, deleting, or updating links, and executing requested tasks.

---

### Dynamic Layer-2 Fabric

Node-to-node links, including inter-satellite links (ISLs) and satellite-to-ground
links (SGLs), are modeled as VXLAN tunnels that are dynamically created and managed
by each node’s `sat-agent` according to the constellation state stored in Etcd.

This overlay fabric provides transparent Layer-2 connectivity among emulated
nodes, independently of their physical placement within the cluster.

---

### Custom IP Routing

Upon link creation or removal, each `sat-agent` may invoke a user-provided IP
routing module through a Python interface. This module is responsible for updating
the routing daemon or performing custom routing actions over the VXLAN fabric.

A built-in IS-IS routing module for FRR is provided
(see `sat-container/extra/isis.py`), serving both as a reference implementation
and as an example of how routing logic can be integrated with the `sat-agent`.

---

### Automatic IP Addressing and Built-in Name Resolution

NetSatBench can optionally assign IP addresses automatically to all emulated
nodes. When this feature is enabled, the `/etc/hosts` file of each container is
automatically populated with the names and IP addresses of all nodes in the
constellation, enabling name resolution without requiring a dedicated DNS server.

---

### On-board Tasks and Application Execution

User-defined applications and tasks can be executed on any node at specific times
during the emulation, as specified in epoch files. The container image used for
each emulated node (see [sat-container/Dockerfile](sat-container/Dockerfile)) is based on `python:3.11-slim`
and includes additional networking utilities.

Additional software can be installed dynamically by scheduling `apt-get` tasks,
followed by tasks that execute the newly installed applications.

---

### Physics-Driven Networking (Work in Progress)

Link parameters are derived from orbital mechanics and line-of-sight geometry,
enabling realistic and reproducible performance evaluation of satellite network
scenarios.

---

## 📁 Repository Structure

**control/**  
Python scripts implementing constellation-wide orchestration, including cluster configuration and run-time control of constellation evolution.

**sat-container/**  
Software used to build the container image for each emulated node of the satellite system.

**examples/**  
Sample emulated satellite systems used for validation and benchmarking. Configurations are specified in JSON format as described in this [Configuration Manual](docs/configuration.md).

**utils/**  
Utility scripts for analysis, routing and data processing.

**docs/**  
Documentation assets, including images, diagrams, and architectural descriptions.
- [Control Commands](docs/commands.md) — detailed description of the control scripts available in the `control/` directory.
- [Configuration Files](docs/configuration.md) — how to customize JSON files describing the computing system (`worker-config.json`), static data of the constellation (`sat-config.json`), and dynamic constellation behavior (epoch files).
- [Etcd Key-Value Store](docs/etcd.md) — structure and organization of the Etcd key-value store used for constellation state management.
- [Routing Interface](docs/routing-interface.md) — specification of the routing module interface
- [Utils](docs/utils.md) — description of the utility scripts available in the `utils/` directory, including:
    - [constellation-exec](utils/constellation-exec.py), a simplified CLI for executing bash commands within emulated nodes regardless of their working host, similar to the `docker exec` syntax.  
    - [oracle-routing](utils/oracle-routing.py), an ideal L3 shortest path routing with optional drain-before-break functinality.
    - [constellation-stats](utils/constellation-stats.py) for extracting constellation network statistics, such as the presence of network partitions, link drop rates, etc.

---

## 🛠️ Cluster Architecture

The emulation cluster consists of two logical host roles:

- **control host**
- **workers**

A single physical or virtual host may act as both control host and worker.

In typical deployments, control and worker hosts are Linux virtual machines or bare-metal servers connected via a 10 Gbps (or higher) Ethernet network.  
In our experiments, we used OpenStack virtual machines running Ubuntu 24.04.

> **No IP Spoofing**  
> VXLAN tunnels use the IP addresses of the containers’ `eth0` interfaces as tunnel endpoints. Therefore, the underlying network must allow direct IP connectivity between container subnets (`sat-vnet`) across different worker hosts, without IP spoofing protection mechanisms.  
> In cloud environments, this implies that security policies applied to host interfaces must allow unrestricted traffic among all container subnets (`sat-vnet-supernet`).

---

## 📱 Software Requirements

### Control Host

The control host must have SSH access to all workers using key-based authentication.  
It executes orchestration scripts and runs an instance of the **Etcd** key-value store, which maintains the global state of the emulated satellite system.

Required software:
- **Etcd** — distributed key-value store for global state coordination  
- **Python 3** — with dependencies specified in `requirements.txt`  
- **SSH client** — for remote connections to workers  

---

### Worker Hosts

Workers are Linux hosts on which emulated nodes (Linux containers) are instantiated.

Each worker must allow passwordless `sudo` access for the SSH user used by the control host. This enables the execution of required `iptables` commands to permit direct inter-container communication without NAT.

Required software:
- **Docker** — for running containerized emulated nodes. The SSH user must be a member of the `docker` group  
- **SSH server** — to allow remote access from the control host

---

## ⚡ Quick Start

Download or clone the repository on the control host and follow these steps to deploy and run a sample emulated satellite system. Be careful to meet all software requirements on both control and worker hosts previously described.

The sample configuration files are located in [`examples/10nodes`](examples/10nodes).  
The cluster consists of two workers, `host-1` and `host-2`, defined in [`workers-config.json`](examples/10nodes/workers-config.json). For simplicity, `host-1` has also the role of control host.

The emulated system includes 8 satellites, 1 ground station and 1 user, as defined in [`sat-config.json`](examples/10nodes/sat-config.json). IP addressing is automatically managed and IS-IS routing is used for L3 connectivity.

Constellation dynamics (link creation, updates, removal, and task execution) are specified through epoch files located in [`examples/10nodes/constellation-epochs`](examples/10nodes/constellation-epochs). The ground station `gdr1` run an `iperf3` server starting at the initial epoch.

### 1. Customize Configuration
- Mandatory - Edit `workers-config.json` to specify worker IP addresses and SSH parameters.
- Optional - Edit `sat-config.json` to customize static constellation parameters (e.g., node names, container images, etc.).
- Optional - Edit epoch files in `constellation-epochs/` to modify dynamic constellation parameters such as link creation, updates, removal, and task scheduling.

### 2. Cluster Initialization
From the control host, configure the environment variables necessary to access the Etcd store:
```bash
export ETCD_HOST="10.0.1.215" # IP address of the control host, where Etcd runs. Change as needed.
export ETCD_PORT="2379" # Default Etcd client port. Change as needed.

# Optional authentication parameters:
export ETCD_USER="username" # Etcd username, if authentication is enabled. Change as needed.
export ETCD_PASSWORD="password" # Etcd password, if authentication is enabled. Change as needed.
export ETCD_CA_CERT="/path/to/ca.crt" # Path to Etcd CA certificate, if TLS is enabled. Change as needed.

```

Initialize the workers' network and computing environment to prepare them for hosting emulated nodes:
```bash
python3 control/system-init-docker.py --config ./examples/10nodes/workers-config.json
```

### 3. Initialize, Deploy and Run the Emulated Satellite System
Execute the `constellation-init.py` script to push the *static* information of constellation in the Etcd key-value store:
```bash
python3 control/constellation-init.py --config ./examples/10nodes/sat-config.json
```
Then, execute the `constellation-deploy.py` script to deploy the emulated satellite nodes across the cluster:
```bash
python3 control/constellation-deploy.py
```
Finally, execute the `constellation-run.py` script to start the emulation by applying scheduled dynamic configuration changes based on the epoch files.
```bash
python3 control/constellation-run.py --loop-delay 60
```
   
### 4. Monitoring and Interaction
You can monitor the status of the emulated nodes and their network by connecting directly to the containers running on the worker hosts via SSH.
The easier way is to use the `utils/constellation-exec.py` script, which simplifies the connection process.

For example: 
- to run a bash on a satellite container named `usr1` use:
    ```bash
    python3 utils/constellation-exec.py -it usr1 bash
    ```

- to check the status of forwarding table on satellite `usr1` use:
```bash
python3 utils/constellation-exec.py usr1 ip route show\
```

- to run an iperf3 client from user `usr1` to ground station `grd1` use:
```bash
python3 utils/constellation-exec.py usr1 iperf3 -c grd1 -t 30 -i 2
```


### 5. Cleanup
After completing your experiments, you can remove the emulated satellite system by running the `constellation-rm.py` script.
This script removes all containers from the worker hosts and clears the Etcd state:
```bash
python3 control/constellation-rm.py
```
Optionally, you may run the `system-cleanup-docker.py` script to remove any residual configuration from the worker hosts.
This step is required only if you plan to run another emulation with different worker settings; otherwise, it can be skipped:
```bash
python3 control/system-cleanup-docker.py
```
