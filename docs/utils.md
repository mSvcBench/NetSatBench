<div align="center">
<img src="images/netsatbench_logo.png" alt="NetSatBench Logo" width="200"/>

# Utility CLI

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

## Overview
This document describes the utility commands provided with NetSatBench for managing and inspecting the emulated satellite network system. These commands are intended to be executed from the control host and interact with the emulation environment by reading from and writing to the central **Etcd** datastore.

## ▶️ Command execution
`nsb.py exec`

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
`nsb.py exectype`

This utility executes the same command on all nodes matching a given node type (for example `satellite`, `gateway`, `user`) by delegating each execution to `nsb-exec`.

Unlike `nsb.py exec`, interactive mode is not supported (`-it` / `--interactive`).

### Usage

```bash
python3 nsb.py exectype [OPTIONS] --node-type <node-type> <command> [args...]
```

### Example

```bash
python3 nsb.py exectype --node-type satellite ip address show
```

To control parallel per-node execution, use `-t/--threads`:

```bash
python3 nsb.py exectype -t 8 --node-type satellite ip address show
```

Full command-line help is available via:
```bash
python3 nsb.py exectype --help
```

---

## 💾 File Copy

`nsb.py cp`

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

`nsb.py cptype`

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

To control parallel per-node copies, use `-t/--threads`:

```bash
python3 nsb.py cptype -t 8 satellite:/var/log ./logs
```

Full command-line help is available via:
```bash
python3 nsb.py cptype --help
```
---

## 🖨️ Dump System Status

`nsb.py status`
This utility script retrieves and displays the current status of the emulated satellite system by reading configuration and state information from Etcd. It provides insights into worker, node deployment status, and link connectivity.
 
### Usage
```bash
python3 nsb.py status -v
```
All parameters are optional since necessary information are retrieved from the data stored in **Etcd**. Run with `--help` to see the full list of available options.

---
## 🖨️ Inspect Node Status
`nsb.py inspect`
This utility script allows inspecting the status of a specific node in the emulated satellite system by retrieving detailed information from Etcd and the corresponding container. It provides insights into node configuration, resource usage, and network connectivity.

### Usage
```bash
python3 nsb.py inspect <node-name> -v
```
Run with `--help` to see the full list of available options.

---

## 📊 Statistics
`nsb.py stats`

This utility script collects and displays statistics from the emulated satellite system.
It retrieves data from Etcd and epoch files and can generate reports on various performance metrics.

---
### Usage  

```bash
python3 nsb.py stats [options]
```
---

## 📌 Inject Run Commands
`nsb.py run-inject`

This utility injects runtime shell commands into the `run` section of the epoch file selected by time. It can target either a single node or node types, using the same `run` structure consumed later by `nsb.py run`.

The selected epoch is the first epoch file whose `time` is greater than or equal to the requested target time. The target time can be provided explicitly with `--target-time`, or derived from the first epoch time plus `--offset-seconds`.

### Usage

```bash
python3 nsb.py run-inject -c <sat-config.json> [--target-time <iso-time> | --offset-seconds <seconds>] [--node <node-name> | --node-type-list <type1,type2,...>] --command-list <command1,command2>
```

### Examples

Inject a command after `2024-06-01T12:00:35Z` for a specific node:

```bash
python3 nsb.py run-inject \
  -c examples/10nodes/sat-config.json \
  --target-time 2024-06-01T12:00:35Z \
  --node grd1 \
  --command-list "screen -dmS iperf iperf3 -s"
```

Inject commands after 120 seconds from the first epoch, mapping each command to a node type:

```bash
python3 nsb.py run-inject \
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
python3 nsb.py run-inject \
  -c examples/10nodes/sat-config.json \
  --target-time 2024-06-01T12:00:35Z \
  --node sat1 \
  --command-list "\"python3 -c \"\"print('a,b')\"\"\",echo done"
```
---

