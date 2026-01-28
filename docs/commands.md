<div align="center">
<img src="images/netsatbench_logo.png" alt="NetSatBench Logo" width="200"/>

# NetSatBench Control Scripts 

</div>

This document describes the control scripts provided with NetSatBench for managing and running satellite network emulations.
The script should be executed from the control host. It is usefull to define the `ETCD_HOST` environment variables to point to the Etcd server used by the emulation.
```bash
export ETCD_HOST=<etcd-server-ip>
```
---

## Worker Initialization  
`control/system-init-docker.py`

This Python script initializes and configures emulation worker nodes using a central **Etcd** datastore.

It performs two main tasks:

1. Injects worker configuration into Etcd  
2. Configures each remote worker host via SSH by setting up Docker networking,
   iptables rules, and container-to-container routing

The script is intended to be executed during the **bootstrap phase** of the compute environment hosting the emulation.

---

### What the Script Does

For each worker defined in the configuration file, the script:

- Stores worker metadata under `/config/workers/` in Etcd
- Connects to the worker via SSH
- Creates (or recreates) a dedicated Docker bridge network with:
  - a unique subnet per worker (`sat-vnet-cidr`)
  - IP masquerading disabled
  - trusted host interface binding
- Ensures packet forwarding is allowed via the `DOCKER-USER` iptables chain for the `sat-vnet-super-cidr`
- Installs static IP routes so that containers running on different workers can reach each other **without NAT**
- Enable NAT for outbound traffic to the Internet from within the containers

The result is a fully connected **Layer-3 container-to-container network** across all workers, which serves as the substrate for creating VXLAN overlay tunnels used to emulate satellite links.

---

### Configuration File

The script expects a JSON configuration file (default: `worker-config.json`), described in the configuration manual (see [configuration.md](configuration.md)).

---

### Usage

Example invocation:

```bash
python3 control/system-init-docker.py \
  --config worker-config.json
```

Run with `--help` to see the full list of available options.


## Worker Cleaning 
`control/system-clean-docker.py`

This Python script cleans up the emulation worker nodes by removing the Docker network and associated iptables rules created during initialization. It is intended to be executed when the emulation is no longer needed, to free up resources on the worker hosts.

It is intended to be executed after the satellite system has been torn down using the `control/constellation-rm.py` script.

---

### Usage
The script read current worker configuration from Etcd under `/config/workers/` and performs the cleanup operations on each worker.
Example invocation:
```bash
python3 control/system-clean-docker.py
```
All parameters are optional since necessary information are retrieved from the data stored in **Etcd**. Run with `--help` to see the full list of available options.

## Constellation Initialization  
`control/constellation-init.py`

This Python script initializes the *static* information about the emulated satellite system (worker nodes, IP addresses, etc. ) in the **Etcd** key–value store based on a satellite configuration file (`sat-config.json`), as described in the configuration manual (see [configuration.md](configuration.md)).

The script prepares all metadata required to deploy the node containers but does **not** start any containers.

It is intended to be executed before deploying the nodes of the satellite system using the `control/constellation-deploy.py` script and after the worker nodes have been initialized using the `control/system-init-docker.py` script.

---

### What the Script Does

For each node defined in the configuration file, the script:

- Automatically selects worker hosts based on available resources.
- Automatically assigns overlay IP addresses and network masks to each node.
- Stores per-node static configuration data in **Etcd** for later consumption
  by other control scripts (e.g., `control/constellation-deploy.py`) and by
  `sat-agent` instances running inside the node containers

The stored metadata doen't include any information on dynamic satellite links, which are instead managed at runtime by the `control/constellation-run.py` script based on the epoch files.

---

### Usage

Example invocation:

```bash
python3 control/constellation-init.py \
  --config ./examples/10nodes/sat-config.json
```

Run with `--help` to see the full list of available options.

## Constellation Deployment  
`control/constellation-deploy.py`

This Python script deploys the satellite system by creating and starting the necessary Docker containers on the worker hosts, based on the system configuration stored in **Etcd**. It is intended to be executed after the satellite system has been initialized using the `control/constellation-init.py` script.

---
### What the Script Does
For each node defined in the satellite system, the script:
- Connects to the assigned worker host via SSH
- Creates and starts a Docker container for the node with the appropriate resource limits and network configuration
- Ensures that each container runs the `sat-agent` process to manage node lifecycle and be ready to contact **Etcd** for dynamic link management during emulation.

---
### Usage
Example invocation:
```bash
python3 control/constellation-deploy.py
```
All parameters are optional since necessary information are retrieved from the data stored in **Etcd**. Run with `--help` to see the full list of available options.

## Constellation Execution
`control/constellation-run.py`

This Python script manages the *dynamic* information of the satellite system, such as links or commands, based on epoch files whose format is described in the configuration manual (see [configuration.md](configuration.md)). 

It is responsible for applying configuration changes, including link additions/removals and command execution within node containers, at the appropriate times during the emulation. It is intended to be executed after the satellite system has been deployed using the `control/constellation-deploy.py` script.

It is intended to be executed after the satellite system has been deployed using the `control/constellation-deploy.py` script.
---
### What the Script Does
The script read epoch files from a specified directory and, for each epoch file:
- Waits until the scheduled epoch time is reached (synchronizing virtual time with real time). 
The waiting time is the time difference between the `time` field in the epoch file and to the `time` field of the first epoch file processed.
- The epoch file names are expected to terminate with a numerical suffix that indicates their processing sequence.  E.g., `NetSatBench-epoch0.json`, `NetSatBench-epoch1.json`, etc.  
- At scheduled time move the epoch file is copied in the epoch-queue directory
- When a new file appear in the epoch-queue directory, the script injects new links and commands (run) information of the epoch file in **Etcd** so that sat-agent instances inside node containers can react accordingly.

The script can be configured to loop indefinitely over the epoch files, restarting the emulation after a specified delay (`--loop-delay`).

The script can be configured to use a fixed wait time between epochs instead of synchronizing virtual time with real time (`--fixed-wait`).

The script can be configured to work in an *interactive-mode* (`--interactive`), where it process only epoch file injected manually by the user in the epoch-queue directory instead of reading from a predefined epoch directory. In this case the script does not perform any time synchronization, simply processing each injected epoch file as soon as it appears in the queue directory. This mode is useful for **digital twin** scenarios where the user update satellite system state at runtime.

---
### Usage
Example invocation:
```bash
python3 control/constellation-run.py \
  --loop-delay 60
```
All parameters are optional since necessary information are retrieved from the data stored in **Etcd**. Run with `--help` to see the full list of available options.

## Constellation Removal
`control/constellation-rm.py`
This Python script removes all information related to the satellite system from the **Etcd** key–value store and removes all Docker containers. 

It is intended to be executed when the emulation is no longer needed, to free up resources on the worker hosts.

---### Usage
Example invocation:
```bash
python3 control/constellation-rm.py
```
All parameters are optional since necessary information are retrieved from the data stored in **Etcd**. Run with `--help` to see the full list of available options. 

