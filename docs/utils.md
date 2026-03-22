<div align="center">
<img src="images/netsatbench_logo.png" alt="NetSatBench Logo" width="200"/>

# NetSatBench Utility Scripts

</div>

## Table of Contents
- [Command execution](#exec-cli)
- [Command execution by type](#command-execution-by-type)
- [File Copy](#file-copy)
- [File Copy by Type](#file-copy-by-type)
- [Dump System Status](#dump-system-status)
- [Inspect Node Status](#inspect-node-status)
- [Statistics](#statistics)
- [Inject Run Commands](#inject-run-commands)
- [Filter Epoch Run Entries](#filter-epoch-run-entries)
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

## ▶️ Command execution by type
`utils/nsb-exectype.py` or `nsb.py exectype`

This utility executes the same command on all nodes matching a given node type (for example `satellite`, `gateway`, `user`) by delegating each execution to `nsb-exec`.

Unlike `nsb.py exec`, interactive mode is not supported (`-it` / `--interactive`).

### Usage

```bash
python3 nsb.py exectype [OPTIONS] <node-type> <command> [args...]
```

### Example

```bash
python3 nsb.py exectype satellite ip address show
```

Full command-line help is available via:
```bash
python3 nsb.py exectype --help
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

## 💾 File Copy by Type

`utils/nsb-cptype.py` or `nsb.py cptype`

This utility copies files between the local host and all nodes of a given type (for example `satellite`, `gateway`, `user`) by delegating each per-node copy to `nsb-cp`.

The syntax is similar to `nsb.py cp`, but uses:

```text
<node-type>:<path>
```

When copying from nodes to the host, output files are prefixed with the node name (for example `sat1_app.log`).

### Usage

```bash
python3 nsb.py cptype [OPTIONS] <src> <dest>
```

### Example

```bash
python3 nsb.py cptype ./config.json satellite:/app/config.json
```

Full command-line help is available via:
```bash
python3 nsb.py cptype --help
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

## 📌 Inject Run Commands
`utils/nsb-run-inject.py`

This utility injects runtime shell commands into the `run` section of the epoch file selected by time. It can target either a single node or node types, using the same `run` structure consumed later by `control/nsb-run.py`.

The selected epoch is the first epoch file whose `time` is greater than or equal to the requested target time. The target time can be provided explicitly with `--target-time`, or derived from the first epoch time plus `--offset-seconds`.

### Usage

```bash
python3 utils/nsb-run-inject.py -c <sat-config.json> [--target-time <iso-time> | --offset-seconds <seconds>] [--node <node-name> | --node-type-list <type1,type2,...>] --command-list <command1,command2>
```

### Examples

Inject a command after `2024-06-01T12:00:35Z` for a specific node:

```bash
python3 utils/nsb-run-inject.py \
  -c examples/10nodes/sat-config.json \
  --target-time 2024-06-01T12:00:35Z \
  --node grd1 \
  --command-list "screen -dmS iperf iperf3 -s"
```

Inject commands after 120 seconds from the first epoch, mapping each command to a node type:

```bash
python3 utils/nsb-run-inject.py \
  -c examples/10nodes/sat-config.json \
  --offset-seconds 120 \
  --node-type-list "usr1,grd1" \
  --command-list "echo starting,sleep 5"
```

### Notes

- The script writes commands into the epoch JSON file and creates a one-time backup next to it as `<epoch-file>.bak` before overwriting the original.
- `--command-list` uses CSV-style parsing. If a single command contains commas, wrap that command in double quotes, and escape inner double quotes by doubling them.
- `--node-type-list` uses CSV-style parsing too, and must contain exactly one entry per command in `--command-list`.

Example:

```bash
python3 utils/nsb-run-inject.py \
  -c examples/10nodes/sat-config.json \
  --target-time 2024-06-01T12:00:35Z \
  --node sat1 \
  --command-list "\"python3 -c \"\"print('a,b')\"\"\",echo done"
```
---

## 🧹 Filter Epoch Run Entries
`utils/filter_epoch_runs.py`

This utility copies a directory of epoch JSON files into a new output directory and removes selected node entries from each epoch's `run` section.

It is useful when you want to reuse an epoch trace but exclude runtime commands for a subset of nodes, for example removing `run.grd1` and `run.grd2` from oracle-routing-generated epochs.

### Usage

```bash
python3 utils/filter_epoch_runs.py --epochs-dir <epochs-dir> --output-dir <output-dir> --nodes <node1,node2,...>
```

### Examples

Remove `grd1` and `grd2` from the `run` sections of the OneWeb delayed-routing epochs and write the filtered copy into a new directory:

```bash
python3 utils/filter_epoch_runs.py \
  --epochs-dir examples/StarPerf/OneWeb/epochs-or-del \
  --output-dir examples/StarPerf/OneWeb/epochs-or-del-filtered \
  --nodes grd1,grd2
```

### Notes

- Only entries under the top-level `run` object are removed. Other references to the same node names, such as `links-add`, `links-del`, or command strings, are preserved.
- If removing the requested nodes leaves an epoch with an empty `run` object, the `run` section is removed entirely from that epoch file.
- Non-JSON files in the source directory are copied to the output directory unchanged.

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
