# ION-DTN Implementation Changes

This note summarizes the NetSatBench changes that support the 10-node ION-DTN
experiment. It describes implementation decisions, not installation steps; use
the [integration guide](../../../docs/ion-dtn-integration-guide.md) to build and
run the lab.

## Command-Line Integration

| File | Change and reason |
| --- | --- |
| `nsb.py` | Registers `ion-prepare`, `ion-generate-configs`, `ion-generate-demo-scenarios`, and `load-image` as normal NetSatBench commands. This keeps ION operations behind the existing CLI dispatcher. |
| `control/nsb-ion-prepare.py` | Generates configuration in a temporary directory, selects scenario nodes, resets stale `/ion` state, copies only each node's files, and optionally restarts ION. Container work uses `--jobs` for bounded parallelism. |
| `utils/nsb-load-image.py` | Saves a local Docker image once, transfers it to selected workers over SSH/SCP, and loads it in parallel. This avoids rebuilding or pulling the ION image separately on every worker. |

## Shared Experiment Time

| File | Change and reason |
| --- | --- |
| `control/nsb-run.py` | Adds `--absolute-start-time` and `--start-delay`. Epoch offsets are calculated from their timestamps and replayed against a shared UTC `T0`, allowing physical NetSatBench links and ION contacts to use the same clock. Absolute scheduling is rejected when `--fixed-wait` is active. |
| `utils/nsb-ion-generate-configs.py` | Renders relative or absolute ION contact times from the same epoch timestamps. This prevents CGR from planning against a timeline different from the emulated links. |

## Contact-Plan Generation

| File | Change and reason |
| --- | --- |
| `utils/nsb-ion-generate-configs.py` | Replaced the original fixed-path generator with registry and epoch driven generation. It validates IPN identities, derives bidirectional ranges and contacts, builds direct-neighbor UDP outducts and IPN plans, and writes per-node `.ionconfig`, `.ionrc`, `.ionsecrc`, `.bprc`, and `.ipnrc` files. |
| `utils/nsb-ion-generate-configs.py` | Parses NetSatBench rates such as `100kbit` and `10mbit` into ION bytes per second. A `links-update` rate change closes the current contact segment and opens a new segment, so planned contact volume follows traffic shaping. |
| `utils/nsb-ion-generate-configs.py` | Parses epoch link delays and renders segment-specific ION ranges. The default `ceil-positive` policy retains legacy whole-second behavior. The experimental subsecond image additionally supports `ceil-millisecond`; physics-derived NetEm delay is then conservatively quantized upward to ION's 1 ms resolution (`4.902ms` becomes `0.005s`) instead of becoming `0s` or `1s`. `owlt_seconds` remains the fallback when an epoch omits `delay`. |
| `utils/nsb-ion-generate-configs.py` | Derives UDP CLA IPv4 addresses from `sat-config.json` using NetSatBench's ordered `/30` allocation rules. Overlay addresses are therefore not duplicated in the ION registry. |
| `utils/nsb-ion-generate-configs.py` | Caps UDP outduct payloads at 1200 bytes, keeping serialized BP fragments below the 1408-byte VXLAN MTU and avoiding IPv4 reassembly loss. |
| `utils/nsb-ion-generate-configs.py` | Allocates 20,000,000 SDR heap words per node so MTU-safe BP fragments do not trip ION heap protection during multi-bundle studies. |
| `examples/10nodes-ion-dtn/ion/ion-node-registry.json` | Replaces `path-spec.json` with ION-oriented terminology: NetSatBench config source, IPN node numbers, default contact rate, OWLT, and default application endpoints. The registry does not hard-code a relay path. |

## Container Runtime

| File | Change and reason |
| --- | --- |
| `sat-container/patches/ion-bpclm-fragment-ordinal-tail.patch` | Updates an urgent ordinal tail when `bpclm` replaces its final queued bundle with contact-capacity fragments. This prevents a later urgent enqueue from dereferencing the removed queue element and poisoning a non-reversible SDR. |
| `sat-container/patches/ion-udpcla-receive-buffer.patch` | Requests a 4 MiB UDP CLA receive buffer, with a portable fallback, so bursts of MTU-safe BP fragments do not overrun relay sockets. |
| `sat-container/patches/ion-bpsendtest-repeat-file-reference.patch` | Keeps the sender's ZCO file reference alive across every `bpsendtest -rep` iteration and releases it once after all payload bundles are queued. |
| `sat-container/stage-ion-runtime.sh` | Copies the required ION executables and shared libraries from a private host installation into a reproducible Docker build context. It requires the sender and SDR diagnostic commands used by quantitative studies. |
| `sat-container/Dockerfile.ion` | Builds the normal NetSatBench node environment with the staged ION runtime, runs `ldconfig`, and retains the existing satellite-agent health check. |
| `control/nsb-deploy.py` | Supports `image-pull-policy` values `always`, `missing`, and `never`, and warns when a node-facing Etcd address is loopback. The ION example uses `never` so Docker does not try to pull its locally built image. |
| `control/nsb-node-restart.py` | Applies the same image-pull policy during node recreation, keeping restart behavior consistent with initial deployment. |

## Link And Route Reliability

| File | Change and reason |
| --- | --- |
| `sat-container/sat-agent.py` | Refactors link application into reusable reconciliation logic. The Etcd watch remains the fast path, while startup and periodic reconciliation repair missed events and remove stale VXLAN interfaces. |
| `sat-container/sat-agent.py` | Derives each peer's overlay address and installs a direct IPv4 `/32 onlink` route on the matching VXLAN interface. UDP CLA traffic can reach an adjacent ION node while dynamic routing is still converging. |
| `sat-container/sat-agent.py` | Adds `NSB_LINK_RECONCILE_INTERVAL`; a non-positive value disables periodic reconciliation when an experiment needs watch-only behavior. |

## Ten-Node Example And Scenarios

| File or directory | Change and reason |
| --- | --- |
| `examples/10nodes-ion-dtn/sat-config.json` | Defines the independent ION-enabled topology with `usr1`, `sat1` through `sat8`, and `grd1`, using the local ION image. The classic `examples/10nodes` configuration remains separate. |
| `examples/10nodes-ion-dtn/worker-config.example.json` | Provides a non-secret single-worker template. Real IP, user, and SSH-key values belong in ignored `worker-config.local.json`. |
| `examples/10nodes-ion-dtn/epochs/` | Contains seven focused scenarios, including the 61-epoch native-clock ION handover comparison. There are 104 generated epoch files in total. |
| `utils/nsb-ion-generate-demo-scenarios.py` | Defines the scenario suite declaratively, writes deterministic epoch JSON, removes stale generated epochs, and provides `--check` for drift detection. |

The complete experiment lives under `examples/10nodes-ion-dtn`, keeping
ION-specific files independent from the classic 10-node example.

## Validation And Documentation

| File | Change and reason |
| --- | --- |
| `test/test_nsb_ion_generate_configs.py` | Tests decimal rate conversion and contact splitting/preservation across `links-update` events. |
| `test/test_ion_demo_scenarios.py` | Checks all seven generated scenarios, the full 10-node registry, derived UDP CLA addresses, dynamic capacity segments, guarded handover overlap, and checked-in epoch consistency. |
| `docs/ion-dtn-integration-guide.md` | Provides the clean-host build, deploy, synchronized run, bundle verification, troubleshooting, cleanup, and multi-worker procedure. |
| `docs/control-commands.md` and `README.md` | Add concise discovery links and command summaries without duplicating the complete guide. |
| `.gitignore` | Excludes ION source/build/runtime artifacts, generated node configs, expanded NetSatBench configs, and local worker credentials. |

## Current Boundary

The generator and NetSatBench replay now share scenario data, rates, and `T0`.
The repository tests verify those static contracts. A live experiment must
still verify that planned contacts match successfully created VXLAN links,
routes, Etcd events, and host clocks; automatic runtime planned-versus-realized
contact auditing is not implemented yet.
