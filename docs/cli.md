<div align="center">
<img src="images/netsatbench_logo.png" alt="NetSatBench Logo" width="200"/>

# NetSatBench Command-Line Interface <!-- omit in toc -->

</div>

## Table of Contents <!-- omit in toc -->
- [Overview](#overview)
- [Worker Initialization](#worker-initialization)
- [Worker Cleaning](#worker-cleaning)
- [Initialization of a satellite system emulation](#initialization-of-a-satellite-system-emulation)
- [Deployment of the nodes](#deployment-of-the-nodes)
- [Reset links and run configurations](#reset-links-and-run-configurations)
- [Restart one or more nodes](#restart-one-or-more-nodes)
- [Execution of the events](#execution-of-the-events)
- [Removal of the emulated satellite system](#removal-of-the-emulated-satellite-system)


## Overview
This document describes the control commands provided with NetSatBench for managing and running satellite network emulations.
The script should be executed from the control host. It is usefull to define the `ETCD_HOST` environment variables to point to the Etcd server used by the control host.
```bash
export ETCD_HOST=<etcd-server-ip>
```
If the Etcd server uses a non-default port (other than `2379`), you can also define the `ETCD_PORT` environment variable:
```bash
export ETCD_PORT=<etcd-server-port>
```

Optionally, if containers emulating nodes of the satellite system should use a different addressing for the Etcd server, you can define the `NODE_ETCD_HOST` and `NODE_ETCD_PORT` environment variables:
```bash
export NODE_ETCD_HOST=<etcd-server-ip-for-nodes>
export NODE_ETCD_PORT=<etcd-server-port-for-nodes>
```


## Worker Initialization  
`nsb.py system-init-docker` or `control/system-init-docker.py`

This Python script initializes and configures emulation worker nodes using a central **Etcd** datastore.

It performs two main tasks:

1. Injects worker configuration into Etcd  
2. Configures each remote worker host via SSH by setting up Docker networking,
   iptables rules, and container-to-container routing

The script is intended to be executed during the **bootstrap phase** of the compute environment hosting the emulation.


### What the Script Does <!-- omit in toc -->

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


### Configuration File <!-- omit in toc -->

The script expects a JSON configuration file (default: `worker-config.json`), described in the configuration manual (see [configuration.md](configuration.md)).


### Usage <!-- omit in toc -->

Example invocation:

```bash
python3 nsb.py system-init-docker \
  --config worker-config.json
```

Run with `--help` to see the full list of available options.


## Worker Cleaning 
`nsb.py system-clean-docker` or `control/system-clean-docker.py`

This Python script cleans up the emulation worker nodes by removing the Docker network and associated iptables rules created during initialization. It is intended to be executed when the emulation is no longer needed, to free up resources on the worker hosts.

It is intended to be executed after the satellite system has been torn down using the `control/nsb-rm.py` script.


### Usage <!-- omit in toc -->
The script read current worker configuration from Etcd under `/config/workers/` and performs the cleanup operations on each worker.
Example invocation:
```bash
python3 nsb.py system-clean-docker
```
All parameters are optional since necessary information are retrieved from the data stored in **Etcd**. Run with `--help` to see the full list of available options.

## Initialization of a satellite system emulation 
`control/nsb-init.py` or `nsb.py init`

This Python script initializes the *static* information about the emulated satellite system (worker nodes, IP addresses, etc. ) in the **Etcd** key–value store based on a satellite configuration file (`sat-config.json`), as described in the configuration manual (see [configuration.md](configuration.md)).

The script prepares all metadata required to deploy the node containers but does **not** start any containers.

It is intended to be executed before deploying the nodes of the satellite system using the `control/nsb-deploy.py` script and after the worker nodes have been initialized using the `control/system-init-docker.py` script.


### What the Script Does <!-- omit in toc -->

For each node defined in the configuration file, the script:

- Automatically selects worker hosts based on available resources.
- Automatically assigns overlay IP addresses and network masks to each node.
- Merges each node with the matching `node-config-common` entries before scheduling and IP assignment.
- Stores per-node static configuration data in **Etcd** for later consumption
  by other control scripts (e.g., `control/nsb-deploy.py`) and by
  `sat-agent` instances running inside the node containers

The stored metadata doen't include any information on dynamic satellite links, which are instead managed at runtime by the `control/nsb-run.py` script based on the epoch files.

If `--write-full-config` is provided, the script also writes an expanded configuration file next to the input config. The output file name is derived from the input one by appending the `-full` suffix before `.json`, for example `sat-config.json` -> `sat-config-full.json`. The generated file keeps the original top-level structure and contains the effective per-node configuration after common-parameter merge, worker scheduling, and IP assignment.


### Usage <!-- omit in toc -->

Example invocation:

```bash
python3 nsb.py init \
  --config ./examples/10nodes/sat-config.json
```

```bash
python3 nsb.py init \
  --config ./examples/10nodes/sat-config.json \
  --write-full-config
```

Run with `--help` to see the full list of available options.

## Deployment of the nodes  
`control/nsb-deploy.py` or `nsb.py deploy`

This Python script deploys the satellite system by creating and starting the necessary Docker containers on the worker hosts, based on the system configuration stored in **Etcd**. It is intended to be executed after the satellite system has been initialized using the `control/nsb-init.py` script.

### What the Script Does <!-- omit in toc -->
For each node defined in the satellite system, the script:
- Connects to the assigned worker host via SSH
- Creates and starts a Docker container for the node with the appropriate resource limits and network configuration
- Ensures that each container runs the `sat-agent` process to manage node lifecycle and be ready to contact **Etcd** for dynamic link management during emulation.


### Usage <!-- omit in toc -->
Example invocation:
```bash
python3 nsb.py deploy
```
All parameters are optional since necessary information are retrieved from the data stored in **Etcd**. Run with `--help` to see the full list of available options.

## Reset links and run configurations
`control/nsb-reset.py` or `nsb.py reset`

This Python script removes all dynamic link state and run configuration stored in **Etcd** under `/config/links/` and `/config/run/`.

It is useful to bring the emulation back to a clean runtime state before starting a new `nsb.py run`, without redeploying containers or reinitializing the full system.

### Usage <!-- omit in toc -->
```bash
python3 nsb.py reset
```
Run with `--help` to see the full list of available options.

## Restart one or more nodes
`control/nsb-node-restart.py` or `nsb.py node-restart`

This Python script recreates and restarts one or more deployed node containers using the node configuration already stored in **Etcd**.

It is intended to be executed from the control host when a node container must be replaced without tearing down the whole emulation. The script:

- reads the node definition from `/config/nodes/<node>`
- resolves the assigned worker from `/config/workers/<worker>`
- connects to the worker via SSH
- removes the existing container if present
- starts a fresh container with the same image, network, and resource settings

The `--node` option accepts a comma-separated list, so multiple nodes can be restarted in one invocation.

### Usage <!-- omit in toc -->
```bash
python3 nsb.py node-restart --node sat1
```

```bash
python3 nsb.py node-restart --node sat1,sat2,usr1
```

Run with `--help` to see the full list of available options.

> Note: Restarting a node abruptly deletes its VXLAN overlay links. This can cause issues in the worker's kernel. It is recommended to remove all VXLAN links before restarting a node.

## Execution of the events 
`control/nsb-run.py` or `nsb.py run`

This Python script manages the *dynamic* information of the satellite system, such as links or commands, based on epoch files whose format is described in the configuration manual (see [configuration.md](configuration.md)). 

It is responsible for applying configuration changes, including link additions/removals and command execution within node containers, at the appropriate times during the emulation. It is intended to be executed after the satellite system has been deployed using the `control/nsb-deploy.py` script.

It is intended to be executed after the satellite system has been deployed using the `control/nsb-deploy.py` script.

### What the Script Does <!-- omit in toc -->
The script read epoch files from a specified directory and, for each epoch file:
- Waits until the scheduled epoch time is reached (synchronizing virtual time with real time). 
The waiting time is the time difference between the `time` field in the epoch file and to the `time` field of the first epoch file processed.
- The epoch file names are expected to terminate with a numerical suffix that indicates their processing sequence.  E.g., `NetSatBench-epoch0.json`, `NetSatBench-epoch1.json`, etc.  
- At scheduled time move the epoch file is copied in the epoch-queue directory
- When a new file appear in the epoch-queue directory, the script injects new links and commands (run) information of the epoch file in **Etcd** so that sat-agent instances inside node containers can react accordingly.

The script can be configured to loop indefinitely over the epoch files, restarting the emulation after a specified delay (`--loop-delay`).

The script can be configured to use a fixed wait time between epochs instead of synchronizing virtual time with real time (`--fixed-wait`).

The script can be configured to work in an *interactive-mode* (`--interactive`), where it process only epoch file injected manually by the user in the epoch-queue directory instead of reading from a predefined epoch directory. In this case the script does not perform any time synchronization, simply processing each injected epoch file as soon as it appears in the queue directory. This mode is useful for **digital twin** scenarios where the user update satellite system state at runtime.

### Usage <!-- omit in toc --><!-- omit in toc -->
Example invocation:
```bash
python3 nsb.py run \
  --loop-delay 60
```
All parameters are optional since necessary information are retrieved from the data stored in **Etcd**. Run with `--help` to see the full list of available options.

## Removal of the emulated satellite system
`control/nsb-rm.py` or `nsb.py rm`
This Python script removes all information related to the satellite system from the **Etcd** key–value store and removes all Docker containers. 

It is intended to be executed when the emulation is no longer needed, to free up resources on the worker hosts.

### Usage <!-- omit in toc -->
Example invocation:
```bash
python3 nsb.py rm
```
All parameters are optional since necessary information are retrieved from the data stored in **Etcd**. Run with `--help` to see the full list of available options. 
