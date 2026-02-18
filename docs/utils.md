<div align="center">
<img src="images/netsatbench_logo.png" alt="NetSatBench Logo" width="200"/>

# NetSatBench Utility Scripts

</div>

## Table of Contents
- [Command execution](#exec-cli)
- [File Copy](#file-copy)
- [Remove all links](#remove-all-links)
- [Dump System Status](#dump-system-status)
- [Inspect Node Status](#inspect-node-status)
- [Statistics](#statistics)
- [Oracle Routing](#oracle-routing-module)

## Overview
This document describes the utility scripts provided with NetSatBench for managing and inspecting the emulated satellite network system. These scripts are intended to be executed from the control host and interact with the emulation environment by reading from and writing to the central **Etcd** datastore.

## ▶️ Command execution
`utils/nsb-exec.py` or `nsb.py exec`

This utility script allows executing commands on emulated satellite nodes by connecting to their respective containers via SSH. The syntax is similar to `docker exec`.

### Usage

```bash
python3 nsb.py exec [-it] [-d] <node-name> <command> [args...]
```

Full command-line help is available via:
```bash
python3 nsb.py exec --help
```

### Examples
- To run a bash shell on a satellite container named `usr1`:
```bash
python3 nsb.py exec -it usr1 bash
```

---

## 💾 File Copy

`utils/nsb-cp.py` or `nsb.py cp`

This utility script allows copying files and directories between the local host and an emulated node by transparently accessing the containers running on remote workers.
Its syntax and behavior closely mimic `docker cp`, while resolving node placement via Etcd and handling remote execution internally.

The copy operation always reads from and writes to the host where `nsb-cp` is executed, preserving standard Docker semantics.

### Usage

```bash
python3 nsb.py cp [OPTIONS] <src> <dest>
```

Where exactly **one** of `<src>` or `<dest>` must be specified in the form:

```text
<node-name>:<path>
```

Full command-line help is available via:
```bash
python3 nsb.py cp --help
```

### Examples

#### Copy a file from a node to the local host

```bash
python3 nsb.py cp sat1:/var/log/app.log ./app.log
```

This copies `/var/log/app.log` from container `sat1` to the current local directory.

#### Copy a local file to a node

```bash
python3 nsb.py cp ./config.json sat1:/etc/app/config.json
```

This transfers `config.json` from the local host into the container filesystem of `sat1`.

#### Copy a directory recursively

```bash
python3 nsb.py cp -r ./configs sat1:/opt/app/configs
```
---

## ✂️ Remove all links
`utils/nsb-unlink.py` or `nsb.py unlink`
Remove all links among nodes of the satellite system by deleting all link entries from Etcd under the `/config/links` prefix.Can be useful to reset the satellite system to a clean state before starting a new emulation run (nsb-run), without the need to redeploy containers or restart the control script.

### Usage
```bash
python3 nsb.py unlink
```
---

## 🖨️ Dump System Status

`utils/nsb-status.py` or `nsb.py status`
This utility script retrieves and displays the current status of the emulated satellite system by reading configuration and state information from Etcd. It provides insights into worker, node deployment status, and link connectivity.
 
### Usage
```bash
python3 nsb.py status -v
```
All parameters are optional since necessary information are retrieved from the data stored in **Etcd**. Run with `--help` to see the full list of available options.

---
## 🖨️ Inspect Node Status
`utils/nsb-inspect.py` or `nsb.py inspect`
This utility script allows inspecting the status of a specific node in the emulated satellite system by retrieving detailed information from Etcd and the corresponding container. It provides insights into node configuration, resource usage, and network connectivity.

### Usage
```bash
python3 nsb.py inspect <node-name> -v
```
Run with `--help` to see the full list of available options.

---

## 📊 Statistics
`utils/nsb-stats.py` or `nsb.py stats`

This utility script collects and displays statistics from the emulated satellite system.
It retrieves data from Etcd and epoch files and can generate reports on various performance metrics.

---
### Usage  

```bash
python3 nsb.py stats [options]
```
---

## 🌍 Oracle Routing Module
`utils/oracle-routing.py`

This module provides a reference **oracle-style routing implementation** for the satellite network emulator. Both IPv4 and IPv6 versions are available, and the desired IP version can be selected via the `--ip-version` command-line parameter.

It demonstrates how routing strategies can be evaluated by injecting explicit routing commands into **epoch files** via `run` sections.

### Key Features

- **Epoch-driven routing control** -
  Routing updates are expressed as `run` commands embedded in epoch files, enabling precise control over when routes are installed or removed relative to link creation and deletion events.

- **Shortest-path routing** -
  Computes hop-count shortest paths from the dynamic network connectivity described in epoch files.

- **Primary and secondary next hops** -
  For each destination, the module installs:
  - a **primary route** with the lowest metric
  - an optional best **secondary route** with a different first hop, when available

- **Drain-before-break support**
  Proactively removes routes that depend on links scheduled for deletion, allowing interface buffers to drain and reducing packet loss during topology changes.


### Architecture and Operation

1. Network topology changes are loaded from epoch JSON files.
2. A dynamic adjacency matrix representing node connectivity is maintained.
3. For each epoch:
   - The adjacency matrix is updated based on link additions and deletions.
   - Dijkstra’s algorithm is executed using the current topology.
   - Primary and secondary next hops are selected per destination.
   - A new epoch file is generated whose timestamp is shifted forward by `--link-creation-offset` seconds.
     This file contains `ip route replace` commands in its `run` section to update routing tables accordingly, after link creation.
   - Optionally, additional *drain-before-break* epoch files are generated prior to link deletion events.

### Drain-Before-Break Behavior
When the `--drain-before-break-offset` parameter is greater than zero:

- Routes that rely on links scheduled for deletion are removed **before** the corresponding link-deletion epoch.
- This is achieved by running Dijkstra on a topology where the soon-to-be-deleted links are already excluded.
- A new epoch file is generated with its timestamp shifted **backward** by the specified offset (in seconds).
- This behavior allows interface queues to drain before teardown, thereby reducing packet loss.

> Note: If no alternative path exists, this approach may temporarily introduce network partitions. Its effectiveness therefore depends on path redundancy in the underlying topology.


### Usage Example

#### 1. Emulation Initialization

Upload the satellite system configuration into Etcd and schedule workers.
In the provided example, the epoch directory is set to `epochs-or` in `sat-config-or.json`.

```bash
python3 control/nsb-init.py -c examples/10nodes/sat-config-or.json
```

#### 2. Node Deployment

Deploy the nodes of the satellite system. Each node registers its overlay IP address in Etcd under `/config/etchosts`.
These addresses are later used by the oracle routing module to generate IP routing commands.

```bash
python3 /home/azureuser/NetSatBench/control/nsb-deploy.py
```

#### 3. Oracle Routing Module Execution

Run the oracle routing module to process epoch files from
`examples/10nodes/epochs` and generate new epoch files with routing commands in
`examples/10nodes/epochs-or`.

In the example below:
- both drain-before-break and link-creation offsets are set to 2 seconds;
- routing rules are generated only for nodes of type `grounds` and `users`.

```bash
python3 utils/oracle-routing.py \
    --epoch-dir examples/10nodes/epochs \
    --out-epoch-dir examples/10nodes/epochs-or \
    --drain-before-break-offset 2 \
    --link-creation-offset 2 \
    --node-type-to-route grounds,users \
    --ip-version 4
```

Full command-line help is available via:
```bash
python3 utils/oracle-routing.py --help
```

#### 4. Execution of events

Run the satellite system emulation using the newly generated epoch files that include routing commands.
The `--loop-delay` option restarts the emulation after 60 seconds, enabling continuous operation.
```bash
python3 nsb.py run --loop-delay 60
```

### Testing and Validation
To validate the oracle routing module, ping tests can be performed between ground stations and user terminals during the satellite system run.
For example, to ping from user node `usr1` to ground station `grd1`:
```bash
python3 nsb.py exec usr1 ping grd1
[... ping output ...]
```