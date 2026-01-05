![Python](https://img.shields.io/badge/Python-3.8%2B-blue?style=for-the-badge&logo=python&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-20.10%2B-2496ED?style=for-the-badge&logo=docker&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)
![Status](https://img.shields.io/badge/Status-Active_Development-orange?style=for-the-badge)

![Python](https://img.shields.io/badge/Python-3.8%2B-blue?style=for-the-badge&logo=python&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-20.10%2B-2496ED?style=for-the-badge&logo=docker&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)
![Status](https://img.shields.io/badge/Status-Active_Development-orange?style=for-the-badge)

<div align="center">

<img src="docs/images/netsatbench_logo.png" alt="NetSatBench Logo" width="200"/>

# **NetSatBench**  
## Large-Scale Satellite Network Benchmarking

</div>

**NetSatBench** is a distributed emulation framework for evaluating communication and application workloads over large-scale satellite constellations. It provides a scalable **Layer-2 (L2) network substrate** on top of which arbitrary routing protocols and applications can be deployed without modification.

Emulated systems consist of satellites, ground stations, and user terminals, each implemented as a **Linux container** and distributed across a cluster of machines. This design enables a high degree of parallelism and scalability.  
VXLAN tunnels dynamically form an **L2 network fabric** interconnecting emulated nodes, with configurable link characteristics (e.g., latency, bandwidth, loss) to reproduce realistic satellite network behavior.

NetSatBench is **Layer-3 and application agnostic**: any routing protocol (e.g., OSPF, BGP, IS-IS) or user-defined application (e.g., `iperf`, traffic generators, analytics workloads) can run unmodified over the emulated constellation. IS-IS routing is supported out of the box via [FRRouting (FRR)](https://frrouting.org/).

---
<div align="center">
## <img src="docs/images/arch_core.png" alt="Architecture icon" width="300"/> Emulation Architecture
</div>

<img src="docs/images/netsatbench-arch.png" alt="NetSatBench System Architecture" width="300"/>

### Distributed Execution and Control
Emulated nodes are instantiated across a cluster of hosts (bare metal or virtual machines), referred to as *workers*. Each node manages its own lifecycle and configuration through an internal agent, called `sat-agent`.  
The `sat-agent` continuously enforces the desired network and computing state stored in an **Etcd distributed key-value store**, whose contents are updated at run time by a dedicated control host to reflect system dynamics.  
The format and semantics of the Etcd keys used by NetSatBench are described in the [JSON format documentation](docs/json-format.md).

### Dynamic L2 Fabric
Node-to-node links—such as inter-satellite links (ISLs) and satellite-to-ground links (SGLs)—are modeled as VXLAN tunnels dynamically created and managed by each node’s `sat-agent`, based on the global system state stored in Etcd.  
This abstraction provides seamless **Layer-2 connectivity**, independent of container placement within the cluster.

### Scalability Through Distribution
By distributing containers across multiple hosts and relying on publish–subscribe coordination via Etcd, NetSatBench scales to thousands of emulated nodes without overloading any single machine.  
Each worker can host multiple containers, each representing a satellite, ground station, or user terminal with its own networking stack and `sat-agent`.

### Built-in Configurable IP Routing Support
FRRouting (FRR) running over the L2 VXLAN fabric is natively supported. Upon link creation or removal, each `sat-agent` may invoke a user-provided routing configuration utility to update the routing daemon or perform protocol-specific actions.  
A built-in IS-IS configuration utility is provided for FRR; its design and integration with the `sat-agent` are described in the [routing interface documentation](docs/routing-interface.md).

### User Application Lifecycle
User applications and tasks can be scheduled for execution inside emulated nodes at specific times defined in the Etcd key-value store.  
Each `sat-agent` continuously monitors a command queue in Etcd and executes user-defined commands inside its container, enabling dynamic application deployment and execution across the emulated satellite system.

### Physics-Driven Networking
Link parameters are derived from orbital mechanics and line-of-sight geometry, enabling realistic and reproducible performance evaluation.

---

## 📁 Repository Structure

**control/**  
Python scripts implementing constellation-wide orchestration, including cluster configuration and run-time control of constellation evolution.

**sat-container/**  
Software used to build the container image for each emulated node of the satellite system.

**test/**  
Sample emulated satellite systems used for validation and benchmarking. Configurations are specified in JSON format as described in this [document](docs/config_format.md).

**docs/**  
Documentation assets, including images, diagrams, and architectural descriptions.

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
- **control/** and **test/** directories — required to run orchestration scripts and define emulated constellations

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

The sample configuration files are located in [`test/10nodes`](test/10nodes).  
The cluster consists of two workers, `host-1` and `host-2`, defined in [`workers-config.json`](test/10nodes/workers-config.json). For simplicity, `host-1` has also the role of control host.

The emulated system includes 9 satellites and 1 ground station, as defined in [`constellation-config.json`](test/10nodes/constellation-config.json).  
Constellation dynamics (link creation, updates, removal, and task execution) are specified through epoch files located in [`test/10nodes/constellation-epochs`](test/10nodes/constellation-epochs). The ground station `gdr1` run an `iperf3` server starting at the initial epoch.

### 1. Customize Configuration
- Edit `workers-config.json` to specify worker IP addresses and SSH parameters.
- Edit `constellation-config.json` to define node placement by setting the `worker` field for each node.

### 2. Cluster Initialization
From the control host, configure the environment variables necessary to access the Etcd store from the control host and the workers:
```bash
export ETCD_HOST="10.0.1.215" # IP address of the control host, where Etcd runs. Change as needed.
export ETCD_PORT="2379" # Default Etcd client port. Change as needed.

# Optional authentication parameters:
export ETCD_USER="username" # Etcd username, if authentication is enabled. Change as needed.
export ETCD_PASSWORD="password" # Etcd password, if authentication is enabled. Change as needed.
export ETCD_CA_CERT="/path/to/ca.crt" # Path to Etcd CA certificate, if TLS is enabled. Change as needed.

```

Configure the worker networking environment:
```bash
python3 control/system-init-docker.py --config ./test/10nodes/workers-config.json
```

### 3. Initialize, Deploy and Run the Emulated Satellite System
Execute the `constellation-init.py` script to initialize the constellation state in the Etcd key-value store:
```bash
python3 control/constellation-init.py --config ./test/10nodes/constellation-config.json
```
Then, execute the `constellation-deploy.py` script to deploy the emulated satellite nodes across the cluster according to the initialization configuration:
```bash
python3 control/constellation-deploy.py
```
Finally, wait a few seconds to ensure that all containers are fully up and running, and then execute the `constellation-run.py` script to start the emulation.
The system state will evolve over time according to the epoch files:
```bash
python3 control/constellation-run.py
```
   
### 4. Monitoring and Interaction
You can monitor the status of the emulated nodes and their network by connecting directly to the containers running on the worker hosts via SSH.
For example, to access a satellite container named `sat-1` on `host-1`, use:
```bash
ssh user@host-1 docker exec -it sat-1 /bin/bash
# inside the container, you can run commands such as:
ip addr show
ip route show
ping grd1
iperf3 -c grd1 # to start an iperf3 client to ground station 1
```
Note that `user` must be replaced with the actual SSH username configured on the worker host.
Within a container, emulated nodes can be referenced by name e\.g\., \`grd1\` for ground station 1.

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