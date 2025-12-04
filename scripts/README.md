## Orchestration and Control Plane

This directory contains the host-side orchestration logic for the NetSatBench simulation environment. These scripts serve as the centralized **Control Plane**, responsible for topology instantiation, configuration synchronization, and runtime network management across distributed physical hosts.

### Architecture Overview

The orchestration layer acts as the bridge between the static topology definition and the dynamic runtime environment. It employs a centralized architecture where the control plane pushes configurations to distributed data plane nodes (containers).

The workflow follows a three-stage pipeline:
1.  **Topology Ingestion:** Parsing declarative JSON definitions and populating the distributed key-value store (Etcd).
2.  **Infrastructure Provisioning:** Instantiating containerized nodes across physical hosts via SSH execution.
3.  **Network Configuration:** Applying Traffic Control (TC) rules for link emulation and injecting Layer 3 routing configurations (IS-IS).

---

###  Core Components

#### `constellation-conf.py` (Topology Synchronization)
*Serves as the source of truth for the simulation state.*
* **Function:** Parses `data-test.json` and synchronizes the network state to the Etcd cluster.
* **Key Operations:**
    * Performs atomic updates to `/config/links` to prevent race conditions during link establishment.
    * Distributes run-time commands to specific nodes via `/config/run/` for automated scenario execution.

#### `constellation-builder.py` (Infrastructure Provisioning)
*Acts as the infrastructure orchestrator.*
* **Function:** Reads the synchronized Etcd state and provisions the physical infrastructure.
* **Key Operations:**
    * Maps logical satellite nodes to physical host IPs.
    * Invokes `create-sat.sh` remotely to instantiate Docker containers with necessary privileges.
    * Validates image versions and SSH reachability prior to deployment.

#### `network-configuration.py` (Network Controller)
*Functions as the Software-Defined Networking (SDN) controller.*
* **Function:** Applies Quality of Service (QoS) rules and configures the routing plane.
* **Key Operations:**
    * **Traffic Control (TC):** Injects `tc qdisc` rules (Token Bucket Filter) into containers to emulate satellite link characteristics (Bandwidth, Burst, Latency).
    * **Routing Logic:** Triggers the internal `configure-isis.sh` script to establish IS-IS adjacencies, assigning ISO System IDs based on node topology.

---

### Configuration & Data

#### `data-test.json`
The declarative topology definition file. It defines:
* **Nodes:** Satellites and Ground Stations (mapped to physical hosts).
* **Links:** Inter-Satellite Links (ISLs) with specific physics attributes (latency, bandwidth).
* **Scenarios:** A list of shell commands (e.g., `ping`, `iperf`) to run automatically upon startup.

---

### Helper Scripts

* **`create-sat.sh`:** A wrapper script executed on remote hosts to launch a single Docker container with volume mounts for SSH keys.
* **`create-sat-bridge.sh`:** Manages the host-level Docker network (`sat-bridge`) and establishes static routes between physical hosts to allow cross-server container communication.
* **`delete-all-sat.py`:** A cleanup utility that queries Etcd for all known nodes and executes remote destruction commands to tear down the simulation.

---

### Usage Workflow

To deploy a full simulation, execute the control plane scripts in the following order:

```bash
# 1. Load the topology into the distributed store
python3 constellation-conf.py

# 2. Provision the containers across the cluster
python3 constellation-builder.py

# 3. Apply physics simulation (TC) and configure routing (IS-IS)(Optional)
python3 network-configuration.py