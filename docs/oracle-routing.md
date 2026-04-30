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

1. It updates an internal graph using `links-add`, `links-update` (for delay metric), and `links-del`.
2. It runs shortest-path computation (Dijkstra) on the current graph.
3. It selects primary next hop (and optional secondary next hop when redundancy is enabled).
4. It emits route commands in the epoch `run` section for target nodes.
5. It writes processed epoch files to an output directory.

Optional drain-before-break behavior can emit additional earlier epochs that remove routes dependent on links about to be deleted.

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
- `--node-type-no-forward`: Node types treated as non-forwarding hosts (default: `user`).
- `--routing-metrics`: `hops` or `delay`.
- `--link-delay-quantum-ms`: Delay quantization used in delay metric mode.
- `--ip-version`: `4` or `6`.
- `--redundancy`: Enable primary + secondary next-hop generation.
- `--link-creation-offset`: Seconds to delay route installation after link creation.
- `--drain-before-break-offset`: Seconds to pre-install route changes before link deletion.
- `--max-routes-per-epoch`: Batch size before inserting sleep in command stream.
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

