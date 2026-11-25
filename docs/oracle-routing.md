<div align="center">
<img src="images/netsatbench_logo.png" alt="NetSatBench Logo" width="200"/>

# Oracle Routing Utility

</div>

---

## Table of Contents
- [Oracle Routing Utility](#oracle-routing-utility)
  - [Table of Contents](#table-of-contents)
  - [Overview](#overview)
  - [Purpose in NetSatBench](#purpose-in-netsatbench)
  - [How It Works](#how-it-works)
  - [Node Roles and Graph Pruning](#node-roles-and-graph-pruning)
  - [Command-Line Interface](#command-line-interface)
  - [Typical Workflow](#typical-workflow)

---

## Overview

`utils/oracle-routing.py` is an offline routing pre-processor that reads NetSatBench epoch files, computes shortest-path routes for each epoch, and writes a new epoch sequence with `run` JSON keys that install routes inside node containers.

The utility supports both IPv4 and IPv6 route generation and can optionally generate secondary next-hop routes for redundancy.

---

## Purpose in NetSatBench

In the NetSatBench architecture, epoch files represent time-varying topology, while route updates can be injected as commands in epoch `run` sections. This utility automates that process by converting dynamic link-state snapshots into route-installation commands.

---

## How It Works

For each input epoch:

1. It updates an internal graph using `links-add`, `links-update` (for delay metric), and `links-del`. Link events whose endpoints are not in the graph are ignored (see [Node Roles and Graph Pruning](#node-roles-and-graph-pruning)).
2. If the graph did not change, the epoch is copied as-is and no route computation is performed. With the `hops` metric `links-update` events are never considered, so an epoch containing only updates never triggers a recomputation.
3. Otherwise, it runs shortest-path computation (Dijkstra) towards the route target nodes only. Non-forwarding nodes (`--node-type-no-forward`) may be path endpoints but are never used as transit hops.
4. It selects primary next hop (and optional secondary next hop when redundancy is enabled), with a small anti-flap bias towards the previously installed next hops. Only next hops that actually changed produce route commands.
5. It emits route commands in the epoch `run` section for target nodes.
6. It writes processed epoch files to an output directory.

Link weights are `1` (hops) or the quantized delay (delay metric), plus a large penalty (`4096`) for links between nodes of different types, so that paths stay within the same node type whenever possible (e.g., satellite-to-satellite routes avoid relaying through gateways).

Optional drain-before-break behavior can emit additional earlier epochs that remove routes dependent on links about to be deleted. In that case link deletions are applied (and routes recomputed) in the drain-before-break epoch, so the following original epoch recomputes routes only if it also adds (or, with the delay metric, updates) links.

---

## Node Roles and Graph Pruning

Each node type plays one or more of the following roles:

| Option | Role |
|---|---|
| `--node-type` | Node types included in the routing graph. Links with an endpoint outside these types are ignored. |
| `--node-type-to-route` | Destinations: routes are computed towards these nodes. Defaults to `--node-type`. |
| `--node-type-to-install` | Sources: route commands are installed on these nodes. Defaults to `--node-type`. |
| `--node-type-no-forward` | Hosts without IP forwarding: may be source or destination of a path, never a transit hop. Default: `user`. |

A node whose type is in `--node-type-no-forward` and that is neither a route destination nor an install point can never appear on a computed path, so it is removed from the graph and its link events are ignored.

Pruning the graph matters for performance: any change on a link of a node in the graph triggers a recomputation, even when that node is only a potential (high-penalty) transit. For instance, to compute routes among satellites only, either restrict the graph:

```bash
--node-type satellite --node-type-to-route satellite --node-type-to-install satellite
```

or declare the other types as non-forwarding:

```bash
--node-type-to-route satellite --node-type-to-install satellite --node-type-no-forward user,gateway
```

Both give the same routes; without either option, gateway links stay in the graph (gateways could relay between disconnected satellite partitions) and every gateway link add/delete triggers a recomputation.

---

## Command-Line Interface

Basic syntax:

```bash
python3 utils/oracle-routing.py [options]
```

Main options:

- `--etcd-host`, `--etcd-port`, `--etcd-user`, `--etcd-password`, `--etcd-ca-cert`: Etcd connection parameters.
- `--epoch-dir`: Input epoch directory (overrides Etcd epoch config).
- `--file-pattern`: Input epoch filename pattern.
- `--out-epoch-dir`: Output directory for generated epochs.
- `--report`: Emit a JSON report with routing update statistics.
- `--node-type`: Node types included in the graph (default: `any`).
- `--node-type-to-route`: Destination node types for which routes are generated.
- `--node-type-to-install`: Node types where route commands are installed.
- `--node-type-no-forward`: Node types treated as non-forwarding hosts, never used as transit hops (default: `user`).
- `--routing-metrics`: `hops` or `delay`.
- `--link-delay-quantum-ms`: Delay quantization used in delay metric mode.
- `--ip-version`: `4` or `6`.
- `--redundancy`: Enable primary + secondary next-hop generation.
- `--link-creation-offset`: Seconds after link creation at which the routes of the epoch are installed (default: `1`).
- `--drain-before-break-offset`: Seconds to pre-install route changes before link deletion.
- `--max-routes-per-command`: Maximum number of routes per `run` entry (command); a sleep entry is inserted between entries.
- `--route-batch-sleep-seconds`: Sleep between route batches.
- `--log-level`: Logging verbosity.

---

## Typical Workflow

1. Initialize NetSatBench scenario state with a config file that points to the epoch directory created by oracle routing:

```bash
python3 nsb.py init --config examples/10nodes/sat-config-or.json
```

2. Deploy nodes so node addresses are available in Etcd and can be used by oracle routing to generate next-hop IPs:

```bash
python3 nsb.py deploy -t 8
```

3. Run oracle route generation over baseline epoch files:

```bash
python3 utils/oracle-routing.py \
  --epoch-dir examples/10nodes/epochs \
  --file-pattern 'NetSatBench-epoch*.json' \
  --out-epoch-dir examples/10nodes/epochs-or \
  --node-type-to-route any \
  --node-type-to-install any \
  --ip-version 4 \
  --routing-metrics hops \
  --link-creation-offset 2 \
  --drain-before-break-offset 2
```

4. Run emulation:

```bash
python3 nsb.py run
```

---

Show full help:

```bash
python3 utils/oracle-routing.py --help
```

