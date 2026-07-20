# ION-DTN Implementation Notes

This note records the current ION-DTN integration state for the NetSatBench
10-node example.

## Scope

The integration adds Bundle Protocol testing on top of the existing NetSatBench
overlay. NetSatBench still owns container deployment, VXLAN link changes, direct
neighbor routes, and epoch replay. ION runs inside the selected containers and
uses the generated contact plan for BP/IPN forwarding.

Default DTN application flow:

```text
usr1 -> grd1
```

The relay path is scenario-derived. `path-spec.json` only defines the node
registry, ION node numbers, overlay IPs, and default source/destination.

## Repository Layout

```text
examples/10nodes/ion-dtn/
|-- README.md
|-- RUNBOOK.md
|-- WHAT_WE_DID.md
`-- path-spec.json

control/
`-- nsb-ion-prepare.py

utils/
`-- nsb-ion-generate-configs.py

examples/10nodes/epochs-ion-dtn-demo/
|-- 00-all-open
|-- 01-store-carry-forward
|-- 02-cloudy-ground
|-- 03-alternate-path
|-- 04-multi-bundle-queue
`-- 05-short-contact-capacity
```

Generated ION configs are not source files. `./nsb.py ion-prepare` creates them
in a temporary directory, copies them into `/ion` inside the selected containers,
starts ION, then removes the host-side temporary files.

## Node Registry

| Node | ION node | Overlay IP | Application endpoint |
| --- | ---: | --- | --- |
| `usr1` | 101 | `172.102.0.2` | `ipn:101.1` |
| `sat1` | 102 | `172.100.0.2` | `ipn:102.1` |
| `sat2` | 103 | `172.100.0.6` | `ipn:103.1` |
| `sat3` | 104 | `172.100.0.10` | `ipn:104.1` |
| `sat4` | 105 | `172.100.0.14` | `ipn:105.1` |
| `sat5` | 106 | `172.100.0.18` | `ipn:106.1` |
| `sat6` | 107 | `172.100.0.22` | `ipn:107.1` |
| `sat7` | 108 | `172.100.0.26` | `ipn:108.1` |
| `sat8` | 109 | `172.100.0.30` | `ipn:109.1` |
| `grd1` | 110 | `172.101.0.2` | `ipn:110.1` |

## Generator Behavior

`./nsb.py ion-generate-configs` reads the node registry in `path-spec.json`
plus a NetSatBench epoch directory.

It derives:

- bidirectional ION ranges from active NetSatBench links,
- bidirectional ION contacts from `links-add` / `links-del`,
- direct-neighbor UDP outducts,
- IPN direct-neighbor plans used by CGR.

The generator does not keep old static exits. Contact windows are derived from
epoch file timestamps, not from file index alone. This keeps ION and
NetSatBench aligned when the run uses one shared absolute T0.

## Runtime Preparation

`./nsb.py ion-prepare` is the preferred entry point. It:

- generates temporary ION configs,
- writes `/ion/ion_nodes`,
- clears stale `/ion` state inside each selected container,
- copies the matching node config into each container,
- starts `ionadmin`, `ionsecadmin`, and `bpadmin`,
- supports `--jobs` for parallel container preparation,
- supports `--absolute-start-time` or `--start-delay` for CGR timing tests.

## NetSatBench Changes

The integration also includes these NetSatBench-side changes:

- `control/nsb-run.py` can replay epochs against a shared absolute start time.
- `control/nsb-deploy.py` and `control/nsb-node-restart.py` support
  `image-pull-policy`, while keeping upstream default pull behavior.
- `examples/10nodes/sat-config.ion.json` selects the ION-enabled image with
  `image-pull-policy: never`.
- `sat-container/sat-agent.py` reconciles direct neighbor routes after link
  changes so ION UDP outduct traffic can follow NetSatBench VXLAN links.
- `sat-container/Dockerfile.ion` and `sat-container/stage-ion-runtime.sh`
  provide the ION-enabled container image path.

## Validation State

The core DTN scenarios have been exercised:

```text
00-all-open              pass
01-store-carry-forward   pass
02-cloudy-ground         pass
03-alternate-path        pass
04-multi-bundle-queue    pass
05-short-contact-capacity boundary/stress scenario
```

`05-short-contact-capacity` is intentionally a capacity boundary test. Small
payloads should pass. Oversized payloads may leave partial acquisition files on
the receiver instead of producing a complete `testfileN`; that is expected for
the stress case.
