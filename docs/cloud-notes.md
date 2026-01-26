<div align="center">
<img src="images/netsatbench_logo.png" alt="NetSatBench Logo" width="200"/>
</div>

# Oracle Routing Module

This module provides a reference **oracle-style routing implementation** for the satellite network emulator.
It demonstrates how routing strategies can be evaluated by injecting explicit routing commands into **epoch files** via `run` sections.

---

## Key Features

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

---

## Architecture and Operation

1. Network topology changes are loaded from epoch JSON files.
2. A dynamic adjacency matrix representing node connectivity is maintained.
3. For each epoch:
   - The adjacency matrix is updated based on link additions and deletions.
   - Dijkstra’s algorithm is executed using the current topology.
   - Primary and secondary next hops are selected per destination.
   - A new epoch file is generated whose timestamp is shifted forward by `--link-creation-offset` seconds.
     This file contains `ip route replace` commands in its `run` section to update routing tables accordingly, after link creation.
   - Optionally, additional *drain-before-break* epoch files are generated prior to link deletion events.

---

## Drain-Before-Break Behavior
When the `--drain-before-break-offset` parameter is greater than zero:

- Routes that rely on links scheduled for deletion are removed **before** the corresponding link-deletion epoch.
- This is achieved by running Dijkstra on a topology where the soon-to-be-deleted links are already excluded.
- A new epoch file is generated with its timestamp shifted **backward** by the specified offset (in seconds).
- This behavior allows interface queues to drain before teardown, thereby reducing packet loss.

> Note: If no alternative path exists, this approach may temporarily introduce network partitions. Its effectiveness therefore depends on path redundancy in the underlying topology.

---

## Usage Example

### 1. Constellation Initialization

Upload the satellite constellation configuration into Etcd and schedule workers.
In the provided example, the epoch directory is set to `constellation-epochs-or` in `sat-config-or.json`.

```bash
python3 control/constellation-init.py -c examples/10nodes/sat-config-or.json
```

---

### 2. Constellation Deployment

Deploy the constellation.
Each node registers its overlay IP address in Etcd under `/config/etchosts`.
These addresses are later used by the oracle routing module to generate IP routing commands.

```bash
python3 /home/azureuser/NetSatBench/control/constellation-deploy.py
```

---

### 3. Oracle Routing Module Execution

Run the oracle routing module to process epoch files from
`examples/10nodes/constellation-epochs` and generate new epoch files with routing commands in
`examples/10nodes/constellation-epochs-or`.

In the example below:
- both drain-before-break and link-creation offsets are set to 2 seconds;
- routing rules are generated only for nodes of type `grounds` and `users`.

```bash
python3 utils/oracle-routing.py \
    --epoch-dir examples/10nodes/constellation-epochs \
    --out-epoch-dir examples/10nodes/constellation-epochs-or \
    --drain-before-break-offset 2 \
    --link-creation-offset 2 \
    --node-type-to-route grounds,users
```

Full command-line help is available via:
```bash
python3 utils/oracle-routing.py --help
```
---

### 4. Constellation Execution

Run the constellation emulation using the newly generated epoch files that include routing commands.
The `--loop-delay` option restarts the emulation after 60 seconds, enabling continuous operation.
```bash
python3 control/constellation-run.py --loop-delay 60
```
---
### Testing and Validation
To validate the oracle routing module, ping tests can be performed between ground stations and user terminals during the constellation run.
For example, to ping from user node `usr1` to ground station `grd1`:
```bash
python3 utils/constellation-exec.py usr1 ping grd1
[... ping output ...]
```